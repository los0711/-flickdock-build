"""Inline account controls. Credentials remain only in the temporary form."""
from __future__ import annotations

from functools import partial
import os
from pathlib import Path
import sys
from account_service import valid_new_password
from native_ui_common import reference_font, icon as ui_icon, colors as ui_colors

from PySide6.QtCore import QByteArray, QDateTime, QEvent, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QLinearGradient
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QToolButton,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget, QLayout)


def _label(text='', parent=None, *, muted=False):
    label=QLabel(str(text),parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
    if muted:label.setObjectName('accountMuted')
    return label


class PasswordEdit(QLineEdit):
    """One native trailing action; no duplicated password value or logging."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self._visibility_theme='light'
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.visibility_action=self.addAction(ui_icon('eye'),QLineEdit.ActionPosition.TrailingPosition)
        self.visibility_action.setCheckable(True)
        self.visibility_action.triggered.connect(self._set_revealed)
        self.textChanged.connect(self._text_changed)
        self.set_visibility_theme('light')

    def _text_changed(self,text):
        if not text:self._set_revealed(False)

    def _set_revealed(self,revealed):
        cursor=self.cursorPosition();start=self.selectionStart();length=len(self.selectedText())
        modified=self.isModified()
        self.setEchoMode(QLineEdit.EchoMode.Normal if revealed else QLineEdit.EchoMode.Password)
        self.visibility_action.setChecked(revealed)
        if start>=0:
            self.setSelection(start+length,-length) if cursor==start else self.setSelection(start,length)
        else:self.setCursorPosition(cursor)
        self.setModified(modified);self._refresh_eye()

    def _refresh_eye(self):
        revealed=self.echoMode()==QLineEdit.EchoMode.Normal
        title='隐藏密码' if revealed else '显示密码'
        self.visibility_action.setText(title);self.visibility_action.setToolTip(title)
        self.visibility_action.setIcon(ui_icon('eye-off' if revealed else 'eye',ui_colors(self._visibility_theme)['secondary']))
        for button in self.findChildren(QToolButton):
            button.setAccessibleName(title);button.setCursor(Qt.PointingHandCursor)

    def set_visibility_theme(self,theme):
        self._visibility_theme=theme;c=ui_colors(theme)
        for button in self.findChildren(QToolButton):
            button.setStyleSheet(f"QToolButton{{background:transparent;border:0;border-radius:5px;padding:3px;}} QToolButton:hover{{background:{c['hover']};}} QToolButton:pressed{{background:{c['selected']};}}")
        self._refresh_eye()


class AuthStatusLabel(QLabel):
    """Keep wrapped errors measurable through the nested modal scroll area."""
    def __init__(self,parent=None):
        super().__init__('',parent)
        self.setObjectName('accountMuted');self.setTextFormat(Qt.PlainText);self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        from PySide6.QtCore import QPropertyAnimation,QEasingCurve
        self._opacity=QGraphicsOpacityEffect(self);self.setGraphicsEffect(self._opacity)
        self._fade=QPropertyAnimation(self._opacity,b'opacity',self);self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

    def _fit_height(self):
        height=self.heightForWidth(self.width())
        if height!=self.minimumHeight() or self.maximumHeight()!=height:self.setFixedHeight(height);self.updateGeometry()

    def hasHeightForWidth(self):return False

    def heightForWidth(self,width):
        return max(self.fontMetrics().height(),self.fontMetrics().boundingRect(QRect(0,0,max(1,width),10000),Qt.TextWordWrap,self.text()).height()) if self.text() else 0

    def setText(self,text):
        super().setText(text);self._fit_height()

    def resizeEvent(self,event):
        super().resizeEvent(event);self._fit_height()

    def showEvent(self,event):
        super().showEvent(event);self._fit_height()
        from native_ui_common import reduced_motion
        self._fade.stop()
        if reduced_motion():self._opacity.setOpacity(1)
        else:self._fade.setStartValue(.2);self._fade.setEndValue(1);self._fade.start()


class AccountNameLabel(QLabel):
    """Keep the full account name for assistive tech and elide its one-line view."""
    def __init__(self, parent=None):
        super().__init__('', parent)
        self._full_text=''
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setFixedHeight(26)

    def text(self):
        return self._full_text

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full_text)+3,self.height())

    def minimumSizeHint(self):
        return QSize(0,self.height())

    def setText(self, value):
        self._full_text=str(value)
        self.setAccessibleName(self._full_text)
        self._refresh_display()

    def clear(self):
        self.setText('')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_display()

    def _refresh_display(self):
        width=max(1,self.contentsRect().width())
        super().setText(self.fontMetrics().elidedText(self._full_text,Qt.TextElideMode.ElideRight,width))


def _button(text, parent=None, *, name=''):
    button=QPushButton(text,parent)
    button.setAutoDefault(False);button.setDefault(False)
    button.setMinimumHeight(34)
    if name:button.setObjectName(name)
    return button


class RemoveDeviceButton(QPushButton):
    def __init__(self,parent=None,*,rename=False):
        super().__init__(parent)
        self.dark=False
        self.rename=rename
        self.setObjectName('accountRemoveDevice')
        self.setFixedSize(32,32)
        self.setAutoDefault(False);self.setDefault(False)
        self.setToolTip('修改设备名称' if rename else '移除登录')
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self,event):
        super().paintEvent(event)
        if self.text():return
        color=('#64B5FF' if self.dark else '#007AFF') if self.rename else ('#FF453A' if self.dark else '#D63230')
        path=('m11.5 3.5 3 3M3 15l3.4-.7L15 5.7a1.1 1.1 0 0 0 0-1.5l-1.2-1.3a1.1 1.1 0 0 0-1.6 0L3.7 11.5 3 15' if self.rename
              else 'M7 3H3v12h4M10 5l4 4-4 4M6 9h8')
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><path d="{path}" fill="none" stroke="{color}" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        painter=QPainter(self)
        if not self.isEnabled():painter.setOpacity(.4)
        QSvgRenderer(QByteArray(svg.encode('utf8'))).render(painter,QRectF((self.width()-18)/2,(self.height()-18)/2,18,18))


class AccountGlyph(QWidget):
    """A single vector family for the account and device identity markers."""
    def __init__(self,parent=None,*,device=False,platform=''):
        super().__init__(parent)
        self.device=device;self.platform=platform;self.dark=False
        self.initial='•'
        self.setFixedSize(34 if device else 44,34 if device else 44)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.device:
            gradient=QLinearGradient(0,self.height(),self.width(),0)
            gradient.setColorAt(0,QColor('#0A84FF'));gradient.setColorAt(1,QColor('#BF5AF2'))
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(gradient);painter.drawEllipse(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5))
            font=self.font();font.setPixelSize(14);font.setWeight(QFont.Weight.Bold);painter.setFont(font)
            painter.setPen(QColor('#FFFFFF'))
            if self.initial:painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,self.initial)
            else:ui_icon('user','#FFFFFF').paint(painter,(self.width()-22)//2,(self.height()-22)//2,22,22)
            return
        tint=ui_colors('dark' if self.dark else 'light')['secondary']
        size=min(20,max(0,min(self.width(),self.height())-4))
        glyph='apple' if self.platform=='macos' else 'windows' if self.platform=='windows' else 'desktop'
        ui_icon(glyph,'#0A84FF' if glyph=='windows' else tint).paint(
            painter,(self.width()-size)//2,(self.height()-size)//2,size,size)


class AccountPanel(QFrame):
    """AccountService presentation, embedded in SettingsDialog's account slot."""
    height_changed=Signal()
    def hasHeightForWidth(self):return True

    def heightForWidth(self,width):
        if not hasattr(self,'signed_in_page') or self.signed_in_page.isHidden():
            return self.layout().totalHeightForWidth(width) if self.layout() else -1
        margins=self.layout().contentsMargins()
        height=self.signed_in_page.minimumHeight()+margins.top()+margins.bottom()+2
        if self.status_label.parentWidget() is self and not self.status_label.isHidden():
            height+=max(self.status_label.sizeHint().height(),self.status_label.heightForWidth(max(1,width-34)))+self.layout().spacing()
        return height

    def sizeHint(self):
        size=super().sizeHint()
        if hasattr(self,'signed_in_page') and not self.signed_in_page.isHidden():size.setHeight(self.heightForWidth(self.width()))
        return size

    def __init__(self,service,parent=None,*,theme='light',email_binding=False):
        super().__init__(parent)
        self.service=service
        self._fit_timer=QTimer(self);self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_signed_content)
        # Keep the argument for older app builds; the email controls are gone.
        self.email_binding=False
        self.setObjectName('accountPanel')
        self.setFont(reference_font())
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        self._mode='login';self._account_id='';self._busy=bool(service.busy)
        self._devices={};self.device_buttons={};self._device_rows=[];self._rows_by_id={}
        self.device_rename_buttons={};self.device_name_edits={};self._rename_forms={}
        self._rename_save_buttons={};self._editing_device=''
        self._confirm_device='';self._revoke_pending='';self._refreshing=False
        self._dark=False
        layout=QVBoxLayout(self);layout.setContentsMargins(16,14,16,14);layout.setSpacing(12)
        heading=QHBoxLayout()
        title=_label('账号与同步',self);title.setObjectName('accountTitle')
        heading.addWidget(title,1)
        self.account_state_label=_label('未登录',self,muted=True)
        self.account_state_label.setObjectName('accountState')
        self.account_state_label.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Preferred)
        heading.addWidget(self.account_state_label)
        layout.addLayout(heading)

        self.auth_page=QWidget(self);auth=QVBoxLayout(self.auth_page);auth.setContentsMargins(0,0,0,0);auth.setSpacing(9)
        tabs=QHBoxLayout();tabs.setSpacing(6)
        self.login_tab=_button('登录',self.auth_page,name='accountTab')
        self.register_tab=_button('注册',self.auth_page,name='accountTab')
        for button in (self.login_tab,self.register_tab):button.setCheckable(True);tabs.addWidget(button)
        tabs.addStretch();auth.addLayout(tabs)
        self.login_tab.clicked.connect(lambda:self.set_auth_mode('login'))
        self.register_tab.clicked.connect(lambda:self.set_auth_mode('register'))
        auth.addWidget(_label('账号',self.auth_page))
        self.username_edit=QLineEdit(self.auth_page);self.username_edit.setPlaceholderText('请输入账号')
        self.username_edit.setAccessibleName('账号');self.username_edit.setMaxLength(128)
        auth.addWidget(self.username_edit)
        auth.addWidget(_label('密码',self.auth_page))
        self.password_edit=PasswordEdit(self.auth_page)
        self.password_edit.setAccessibleName('密码')
        auth.addWidget(self.password_edit)
        self.submit_button=_button('登录',self.auth_page,name='accountPrimary')
        self.submit_button.clicked.connect(self._submit_auth);auth.addWidget(self.submit_button)
        self.auth_status_label=AuthStatusLabel(self.auth_page)
        self.auth_status_label.setAccessibleName('账号登录状态')
        auth.addWidget(self.auth_status_label)
        layout.addWidget(self.auth_page)

        self.signed_in_page=QWidget(self);signed=QVBoxLayout(self.signed_in_page)
        signed.setContentsMargins(0,0,0,0);signed.setSpacing(12)
        self.profile_frame=QFrame(self.signed_in_page)
        self.profile_frame.setObjectName('accountProfile')
        identity=QHBoxLayout(self.profile_frame)
        identity.setContentsMargins(12,12,12,12);identity.setSpacing(10)
        self.account_glyph=AccountGlyph(self.signed_in_page)
        identity.addWidget(self.account_glyph)
        identity_text=QVBoxLayout();identity_text.setSpacing(3)
        self.username_label=AccountNameLabel(self.signed_in_page)
        self.username_label.setObjectName('accountUsername')
        identity_text.addWidget(self.username_label)
        self.login_alias_edit=QLineEdit(self.signed_in_page)
        self.login_alias_edit.setReadOnly(True);self.login_alias_edit.setMaxLength(128)
        self.login_alias_edit.setAccessibleName('重名账号登录 ID')
        self.login_alias_edit.hide();identity_text.addWidget(self.login_alias_edit)
        self.sync_status_label=_label('',self.signed_in_page,muted=True)
        self.sync_status_label.setAccessibleName('云同步状态')
        identity.addLayout(identity_text,1)
        self.logout_button=_button('退出登录',self.signed_in_page,name='accountQuiet')
        self.logout_button.clicked.connect(lambda:self._invoke(self.service.logout));identity.addWidget(self.logout_button)
        signed.addWidget(self.profile_frame)
        signed.addWidget(self.sync_status_label)
        # The live sync result belongs with the identity, while account actions
        # stay in a compact inset group below it.
        self.sync_status_label.setObjectName('accountSyncStatus')
        security_group=QFrame(self.signed_in_page);security_group.setObjectName('accountActions')
        security_group_layout=QVBoxLayout(security_group)
        security_group_layout.setContentsMargins(0,0,0,0);security_group_layout.setSpacing(0)
        security_row=QWidget(security_group)
        security_heading=QHBoxLayout(security_row)
        security_heading.setContentsMargins(12,3,12,3);security_heading.setSpacing(8)
        security_title=_label('账号安全',self.signed_in_page)
        security_title.setObjectName('accountSection')
        security_heading.addWidget(security_title,1)
        self.password_toggle_button=_button('修改密码',self.signed_in_page,name='accountQuiet')
        self.password_toggle_button.setCheckable(True)
        self.password_toggle_button.clicked.connect(self._toggle_password)
        security_heading.addWidget(self.password_toggle_button)
        security_group_layout.addWidget(security_row)
        self.password_form=QFrame(self.signed_in_page);self.password_form.setObjectName('accountSecurity')
        password_layout=QVBoxLayout(self.password_form);password_layout.setContentsMargins(12,12,12,12);password_layout.setSpacing(8)
        for attribute,label,placeholder in (
                ('old_password_edit','原密码','输入当前密码'),
                ('new_password_edit','新密码','至少 6 个字符，不含空白'),
                ('confirm_password_edit','确认新密码','再次输入新密码')):
            password_layout.addWidget(_label(label,self.password_form))
            edit=PasswordEdit(self.password_form)
            edit.setAccessibleName(label);edit.setPlaceholderText(placeholder)
            setattr(self,attribute,edit);password_layout.addWidget(edit)
        password_layout.addWidget(_label('修改后，其他设备需要重新登录。',self.password_form,muted=True))
        password_actions=QHBoxLayout();password_actions.addStretch()
        self.password_cancel_button=_button('取消',self.password_form)
        self.password_change_button=_button('更新密码',self.password_form,name='accountPrimary')
        self.password_cancel_button.clicked.connect(self._reset_password)
        self.password_change_button.clicked.connect(self._change_password)
        password_actions.addWidget(self.password_cancel_button);password_actions.addWidget(self.password_change_button)
        password_layout.addLayout(password_actions);self.password_form.hide()
        signed.addWidget(security_group)
        signed.addWidget(self.password_form)
        devices_header=QHBoxLayout()
        self.devices_heading_label=_label('已登录设备',self.signed_in_page)
        self.devices_heading_label.setObjectName('accountSection')
        devices_header.addWidget(self.devices_heading_label,1)
        self.refresh_button=_button('刷新',self.signed_in_page,name='accountQuiet')
        self.refresh_button.clicked.connect(self.refresh_devices);devices_header.addWidget(self.refresh_button)
        signed.addLayout(devices_header)
        self.devices_body=QFrame(self.signed_in_page)
        self.devices_body.setObjectName('accountDevices')
        self.devices_layout=QVBoxLayout(self.devices_body)
        self.devices_layout.setContentsMargins(4,4,4,4);self.devices_layout.setSpacing(0)
        signed.addWidget(self.devices_body)
        self.confirm_frame=QFrame(self.signed_in_page);self.confirm_frame.setObjectName('accountConfirm')
        confirmation=QVBoxLayout(self.confirm_frame);confirmation.setContentsMargins(12,10,12,10);confirmation.setSpacing(8)
        self.confirm_label=_label('',self.confirm_frame);confirmation.addWidget(self.confirm_label)
        confirm_buttons=QHBoxLayout();confirm_buttons.addStretch()
        self.cancel_remove_button=_button('取消',self.confirm_frame)
        self.confirm_remove_button=_button('移除登录',self.confirm_frame,name='accountDanger')
        self.cancel_remove_button.clicked.connect(self._cancel_remove)
        self.confirm_remove_button.clicked.connect(self._confirm_remove)
        confirm_buttons.addWidget(self.cancel_remove_button);confirm_buttons.addWidget(self.confirm_remove_button)
        confirmation.addLayout(confirm_buttons);signed.addWidget(self.confirm_frame)
        layout.addWidget(self.signed_in_page)
        self.status_label=_label('',self,muted=True);self.status_label.setAccessibleName('账号操作状态')
        layout.addWidget(self.status_label)
        self._build_reference_layout()
        for field in self.findChildren(QLineEdit):
            field.setMinimumWidth(0);field.setMinimumHeight(34)
        for widget in self.findChildren(QWidget):widget.installEventFilter(self)
        service.account_changed.connect(self._account_changed)
        service.status_changed.connect(self._status_changed)
        service.busy_changed.connect(self._busy_changed)
        service.devices_changed.connect(self._devices_changed)
        if hasattr(service,'password_changed'):service.password_changed.connect(self._password_changed)
        self.set_auth_mode('login');self.apply_theme(theme)
        self._account_changed(service.account)
        if service.signed_in:self.refresh_devices()

    @staticmethod
    def _empty_layout(layout):
        while layout.count():
            item=layout.takeAt(0)
            if item.widget():item.widget().hide()
            if item.layout():
                AccountPanel._empty_layout(item.layout());item.layout().deleteLater()

    def _build_reference_layout(self):
        """Use the reference account card while keeping every service binding."""
        root=self.layout();self._empty_layout(root);root.setContentsMargins(16,16,16,16);root.setSpacing(12)
        self.unsigned_profile_button=_button('',self,name='accountUnsignedProfile')
        self.unsigned_profile_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unsigned_profile_button.setMinimumHeight(58)
        unsigned=QHBoxLayout(self.unsigned_profile_button);unsigned.setContentsMargins(12,8,12,8);unsigned.setSpacing(12)
        self.unsigned_glyph=AccountGlyph(self.unsigned_profile_button);self.unsigned_glyph.setFixedSize(40,40)
        self.unsigned_glyph.initial=''
        unsigned.addWidget(self.unsigned_glyph)
        unsigned_text=QVBoxLayout();unsigned_text.setSpacing(2)
        unsigned_name=_label('登录账号',self.unsigned_profile_button);unsigned_name.setObjectName('accountUnsignedTitle')
        unsigned_text.addWidget(unsigned_name);unsigned_text.addWidget(_label('在设备之间同步搁架',self.unsigned_profile_button,muted=True))
        unsigned.addLayout(unsigned_text,1);unsigned.addWidget(_label('›',self.unsigned_profile_button,muted=True))
        self.unsigned_profile_button.clicked.connect(self.show_auth)
        root.addWidget(self.unsigned_profile_button);root.addWidget(self.signed_in_page);root.addWidget(self.status_label)
        signed=self.signed_in_page.layout();self._empty_layout(signed);signed.setSpacing(12);signed.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        signed.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        identity=self.profile_frame.layout();self._empty_layout(identity);identity.setContentsMargins(0,0,0,0);identity.setSpacing(12)
        self.profile_frame.setMinimumHeight(40);self.profile_frame.setMaximumHeight(16777215)
        self.account_glyph.setFixedSize(40,40);identity.addWidget(self.account_glyph);self.account_glyph.show()
        text=QVBoxLayout();text.setSpacing(2);name=QHBoxLayout();name.setSpacing(8)
        self.username_label.setFixedHeight(26);self.account_state_label.setFixedHeight(24)
        self.username_label.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Fixed)
        name.addWidget(self.username_label);name.addWidget(self.account_state_label);name.addStretch();text.addLayout(name)
        text.addWidget(self.sync_status_label)
        identity.addLayout(text,1)
        self.password_toggle_button.setMinimumHeight(28);self.password_toggle_button.setText('修改密码');self.password_toggle_button.show()
        self.logout_button.setMinimumHeight(28);self.logout_button.setObjectName('accountDanger');identity.addWidget(self.password_toggle_button);identity.addWidget(self.logout_button)
        for widget in (self.username_label,self.account_state_label,self.sync_status_label,self.logout_button):widget.show()
        signed.addWidget(self.profile_frame);self.profile_frame.show()
        # The subtitle grows with real sync feedback; the profile has no fixed
        # height, so login results remain fully visible.
        self.sync_status_label.show()
        signed.addWidget(self.login_alias_edit)
        self.profile_frame.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.profile_frame.customContextMenuRequested.connect(self._profile_menu)
        # Devices follow the same two-column inset layout as the source. A row
        # still owns its original rename form and remove-confirmation workflow.
        old=self.devices_body.layout()
        if old is not None:
            from shiboken6 import delete
            delete(old)
        self.devices_layout=QGridLayout(self.devices_body);self.devices_layout.setContentsMargins(0,10,0,0);self.devices_layout.setSpacing(10)
        self.devices_layout.setColumnStretch(0,1)
        self.devices_layout.setColumnStretch(1,1)
        self.devices_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.devices_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.devices_body.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        self.devices_body.installEventFilter(self)
        signed.addWidget(self.devices_body);self.devices_body.show()
        root.removeWidget(self.status_label)
        signed.addWidget(self.status_label)
        self.devices_heading_label.hide();self.refresh_button.hide()
        from app_settings import ReferenceModalDialog
        self.password_dialog=ReferenceModalDialog(self,surface_width=390,modal=True)
        self.password_dialog.setObjectName('accountModal');self.password_dialog.setWindowTitle('修改密码')
        self.password_close=self._modal_header(self.password_dialog,'修改密码',self._reset_password)
        self.password_dialog.body_layout.setContentsMargins(20,10,20,16)
        self.password_dialog.body_layout.setSpacing(10)
        self.password_close.parentWidget().layout().setContentsMargins(0,0,0,6)
        self.password_dialog.body_layout.addWidget(self.password_form)
        password=self.password_form.layout();self._empty_layout(password)
        password.setContentsMargins(0,0,0,0);password.setSpacing(12)
        password.setSizeConstraints(QLayout.SetNoConstraint,QLayout.SetMinimumSize)
        for edit,label,placeholder in ((self.old_password_edit,'原密码','输入原密码'),
                (self.new_password_edit,'新密码 (至少 6 字符且不含空白)','输入新密码'),
                (self.confirm_password_edit,'确认新密码','再次输入新密码')):
            password.addWidget(self._modal_field(edit,label,placeholder,self.password_form))
        self.password_change_button.setText('确认修改')
        password.addWidget(self._modal_actions(self.password_cancel_button,self.password_change_button,self.password_form))
        self.password_status_label=AuthStatusLabel(self.password_form);password.addWidget(self.password_status_label);self.password_status_label.hide()
        self.password_dialog.rejected.connect(self._reset_password)
        auth=self.auth_page.layout();self._empty_layout(auth);auth.setSpacing(12)
        auth.setSizeConstraints(QLayout.SetNoConstraint,QLayout.SetMinimumSize)
        tabs_frame=QFrame(self.auth_page);tabs_frame.setObjectName('accountAuthTabs')
        tabs=QHBoxLayout(tabs_frame);tabs.setContentsMargins(4,4,4,4);tabs.setSpacing(4)
        self.register_tab.setText('注册新账号')
        for button in (self.login_tab,self.register_tab):
            button.setFixedHeight(24);tabs.addWidget(button,1);button.show()
        auth.addWidget(tabs_frame)
        auth.addWidget(self._modal_field(self.username_edit,'账号用户名','请输入账号',self.auth_page))
        auth.addWidget(self._modal_field(self.password_edit,'密码','请输入密码',self.auth_page))
        self.auth_cancel_button=_button('取消',self.auth_page)
        self.auth_cancel_button.clicked.connect(self.close_auth)
        auth.addWidget(self._modal_actions(self.auth_cancel_button,self.submit_button,self.auth_page))
        auth.addWidget(self.auth_status_label);self.auth_status_label.hide()
        self.auth_dialog=ReferenceModalDialog(self,surface_width=390,modal=True)
        self.auth_dialog.setObjectName('accountModal');self.auth_dialog.setWindowTitle('账号管理与登录')
        self.auth_close=self._modal_header(self.auth_dialog,'账号管理与登录',self.close_auth)
        self.auth_dialog.body_layout.addWidget(self.auth_page)
        self.auth_dialog.rejected.connect(self.close_auth)
        self.auth_page.installEventFilter(self);self.password_form.installEventFilter(self)
        self.auth_page.show();self.signed_in_page.show();self.status_label.hide()
        # The measured fixed heights bound wrapping; keep layout ownership of
        # positions so Cocoa and Qt cannot retain different child coordinates.
        signed.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.signed_in_page.setMinimumSize(0,0)

    @staticmethod
    def _modal_field(edit,label,placeholder,parent):
        group=QWidget(parent);layout=QVBoxLayout(group);layout.setContentsMargins(0,0,0,0);layout.setSpacing(4)
        caption=_label(label,group,muted=True);caption.setMinimumHeight(20);layout.addWidget(caption)
        edit.setPlaceholderText(placeholder);edit.setFixedHeight(38);layout.addWidget(edit);edit.show()
        return group

    @staticmethod
    def _modal_actions(cancel,submit,parent):
        group=QWidget(parent);layout=QHBoxLayout(group);layout.setContentsMargins(0,8,0,0);layout.setSpacing(8);layout.addStretch()
        for button in (cancel,submit):button.setMinimumHeight(36);button.setMaximumHeight(36);layout.addWidget(button);button.show()
        return group

    @staticmethod
    def _modal_header(dialog,text,callback):
        from app_settings import SettingsCloseButton
        frame=QFrame(dialog.surface);frame.setObjectName('accountModalHeader')
        header=QHBoxLayout(frame);header.setContentsMargins(0,0,0,12);header.setSpacing(12)
        title=_label(text,frame);title.setObjectName('accountModalTitle');title.setWordWrap(False)
        title.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);header.addWidget(title,1)
        frame.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        close=SettingsCloseButton(frame,glyph_size=14);close.setObjectName('accountModalClose')
        close.setFixedSize(24,24);close.clicked.connect(callback);header.addWidget(close)
        dialog.body_layout.addWidget(frame)
        return close

    def show_auth(self,mode='login'):
        if self.service.signed_in:return
        self.set_auth_mode(mode if mode in ('login','register') else 'login')
        self.auth_page.show();self._fit_auth_form(self.auth_dialog);self.auth_dialog.show()
        self.auth_dialog.raise_();self.auth_dialog.activateWindow();self.username_edit.setFocus()

    def close_auth(self):
        from ui_transitions import fade_hide
        self.password_edit.clear();fade_hide(self.auth_dialog)

    def _profile_menu(self, point):
        menu=QMenu(self.profile_frame)
        self._style_menu(menu)
        refresh=menu.addAction('刷新已登录设备',self.refresh_devices)
        refresh.setEnabled(self.service.signed_in and not self._busy)
        menu.exec(self.profile_frame.mapToGlobal(point))

    def _style_menu(self, menu):
        bg='#1C1C1E' if self._dark else '#FFFFFF';ink='#F5F5F7' if self._dark else '#1D1D1F'
        edge='rgba(255,255,255,20)' if self._dark else 'rgba(0,0,0,23)'
        menu.setStyleSheet(f'QMenu{{background:{bg};color:{ink};border:1px solid {edge};border-radius:10px;padding:6px 0;font-size:12px;min-width:208px;}} QMenu::item{{padding:6px 14px;border:0;}} QMenu::item:selected{{background:#0062CC;color:white;}}')

    def set_auth_mode(self,mode):
        if mode not in ('login','register'):raise ValueError('Unknown account form mode')
        self._mode=mode
        self.login_tab.setChecked(mode=='login');self.register_tab.setChecked(mode=='register')
        self.submit_button.setText('注册并登录' if mode=='register' else '登录账号')
        self.password_edit.setPlaceholderText('至少 6 个字符，不含空白' if mode=='register' else '请输入密码')
        self.password_edit.clear();self.status_label.clear();self.auth_status_label.clear();self.auth_status_label.hide()
        if hasattr(self,'auth_dialog') and self.auth_dialog.isVisible():QTimer.singleShot(0,lambda:self._fit_auth_form(self.auth_dialog))

    def _submit_auth(self):
        if self._busy or self.service.signed_in:return
        if not self.username_edit.text().strip() or not self.password_edit.text():
            self._status_changed('请输入账号和密码。');return
        action=self.service.register if self._mode=='register' else self.service.login
        self._invoke(action,self.username_edit.text().strip(),self.password_edit.text())

    def _invoke(self,action,*args):
        try:return action(*args)
        except (OSError,ValueError,RuntimeError):
            self._status_changed('操作未能完成，请稍后重试。')

    def _account_changed(self,account):
        signed=bool(isinstance(account,dict) and self.service.signed_in)
        identity=str(account.get('id','')) if signed else ''
        if identity!=self._account_id or not signed:
            self._account_id=identity
            self.password_edit.clear();self.username_edit.clear();self.status_label.clear();self.auth_status_label.clear()
            self.username_label.clear();self.login_alias_edit.clear();self.login_alias_edit.hide()
            self.sync_status_label.clear()
            self._reset_password()
            self._refreshing=False;self._revoke_pending=''
            self._devices_changed([])
            if signed:self.sync_status_label.setText('正在准备同步…')
        self.unsigned_profile_button.setVisible(not signed);self.signed_in_page.setVisible(signed)
        self.status_label.setVisible(signed and bool(self.status_label.text()))
        if signed:
            self.close_auth()
            self.password_edit.clear()
            self.username_label.setText(str(account.get('username','')))
            self.username_label.setFixedWidth(min(150,max(28,self.username_label.fontMetrics().horizontalAdvance(self.username_label.text())+3)))
            self.account_glyph.initial=str(account.get('username','•'))[:1].upper();self.account_glyph.update()
            alias=account.get('login_alias')
            self.login_alias_edit.setText(alias if isinstance(alias,str) else '')
            self.login_alias_edit.setVisible(bool(isinstance(alias,str) and alias))
        self._update_enabled()
        self._queue_fit()

    def set_sync_status(self,text):
        if self.service.signed_in:
            self.sync_status_label.setText(str(text)[:240])
            self._queue_fit()

    def _status_changed(self,text):
        message=str(text)
        for edit in (self.password_edit,self.old_password_edit,self.new_password_edit,self.confirm_password_edit):
            secret=edit.text()
            if secret:message=message.replace(secret,'…')
        self.status_label.setText(message[:240]);self.status_label.setVisible(bool(message) and self.service.signed_in)
        self.auth_status_label.setText(message[:240]);self.auth_status_label.setVisible(bool(message) and not self.service.signed_in)
        self.password_status_label.setText(message[:240]);self.password_status_label.setVisible(bool(message) and self.password_dialog.isVisible())
        for dialog in (self.auth_dialog,self.password_dialog):
            if dialog.isVisible():QTimer.singleShot(0,lambda dialog=dialog:self._fit_auth_form(dialog))
        self._refreshing=False;self._revoke_pending='';self._cancel_remove();self._update_enabled()
        self._queue_fit()

    def _fit_auth_form(self,dialog):
        form=self.auth_page if dialog is self.auth_dialog else self.password_form
        status=self.auth_status_label if dialog is self.auth_dialog else self.password_status_label
        status._fit_height()
        layout=form.layout();layout.invalidate()
        height=layout.contentsMargins().top()+layout.contentsMargins().bottom()
        items=[layout.itemAt(index) for index in range(layout.count()) if not layout.itemAt(index).isEmpty()]
        height+=sum(max(item.minimumSize().height(),item.sizeHint().height()) for item in items)+max(0,len(items)-1)*layout.spacing()
        if form.height()!=height or form.minimumHeight()!=height:form.setFixedHeight(height)
        form.updateGeometry();dialog.body_layout.invalidate();dialog.adjustSize()
        area=dialog._available_geometry()
        dialog.move(max(area.left()+8,min(dialog.x(),area.right()-dialog.width()-7)),max(area.top()+8,min(dialog.y(),area.bottom()-dialog.height()-7)))
        if dialog.isVisible() and status.isVisible():dialog.scroll.ensureWidgetVisible(status,0,8)

    def _busy_changed(self,busy):
        self._busy=bool(busy)
        if not self._busy:self.password_edit.clear()
        self._update_enabled()

    def _update_enabled(self):
        signed=bool(self.service.signed_in)
        self.unsigned_profile_button.setEnabled(not signed and not self._busy)
        self.account_state_label.setText('处理中…' if self._busy else '已登录' if signed else '未登录')
        online=signed and not self._busy
        if self.account_state_label.property('online')!=online:
            self.account_state_label.setProperty('online',online)
            self.account_state_label.style().unpolish(self.account_state_label)
            self.account_state_label.style().polish(self.account_state_label)
        for widget in (self.username_edit,self.password_edit,self.submit_button,self.login_tab,self.register_tab):
            widget.setEnabled(not self._busy)
        self.logout_button.setEnabled(signed and not self._busy)
        for widget in (self.password_toggle_button,self.old_password_edit,self.new_password_edit,
                       self.confirm_password_edit,self.password_change_button,self.password_cancel_button):
            widget.setEnabled(signed and not self._busy)
        self.refresh_button.setEnabled(signed and not self._busy and not self._refreshing)
        for button in self.device_buttons.values():button.setEnabled(signed and not self._busy and not self._revoke_pending)
        for button in self.device_rename_buttons.values():button.setEnabled(signed and not self._busy and not self._revoke_pending)
        for key,edit in self.device_name_edits.items():
            edit.setEnabled(signed and not self._busy and not self._revoke_pending)
            self._rename_save_buttons[key].setEnabled(signed and not self._busy and bool(edit.text().strip()))
        self.confirm_remove_button.setEnabled(signed and not self._busy and not self._revoke_pending)

    def _toggle_password(self,checked=False):
        if not self.service.signed_in or self._busy:return
        self.password_form.setVisible(bool(checked))
        if checked:
            self._fit_auth_form(self.password_dialog);self.password_dialog.show();self.password_dialog.raise_();self.password_dialog.activateWindow();self.old_password_edit.setFocus()
        else:self._reset_password()

    def _reset_password(self):
        for edit in (self.old_password_edit,self.new_password_edit,self.confirm_password_edit):edit.clear()
        self.password_toggle_button.setChecked(False);self.password_form.hide()
        from ui_transitions import fade_hide
        fade_hide(self.password_dialog)
        if hasattr(self,'password_status_label'):self.password_status_label.clear();self.password_status_label.hide()

    def _change_password(self):
        if self._busy or not self.service.signed_in:return
        old,new,confirm=self.old_password_edit.text(),self.new_password_edit.text(),self.confirm_password_edit.text()
        if not old:
            self._status_changed('请输入原密码。');self.old_password_edit.setFocus();return
        if not valid_new_password(new):
            self._status_changed('新密码至少需要 6 个字符，不能包含空白。');self.new_password_edit.setFocus();return
        if old==new:
            self._status_changed('新密码需要与原密码不同。');self.new_password_edit.setFocus();return
        if new!=confirm:
            self._status_changed('两次输入的新密码不一致。');self.confirm_password_edit.setFocus();return
        self._invoke(self.service.change_password,old,new,confirm)

    def _password_changed(self):
        self._reset_password()
        self._status_changed('密码已更新，其他设备需要重新登录。')

    def refresh_devices(self):
        if not self.service.signed_in or self._busy or self._refreshing:return
        self._refreshing=True;self._cancel_remove();self._update_enabled()
        self._invoke(self.service.list_devices)

    def _devices_changed(self,devices):
        self._refreshing=False;self._revoke_pending='';self._cancel_remove()
        visible_devices=[dict(device) for device in devices
                         if isinstance(device,dict) and device.get('id') and not device.get('revoked_at')]
        signature=(bool(self.service.signed_in),str(self.service.device_id),visible_devices)
        if (getattr(self,'_rendered_devices_signature',None)==signature
                and not self._editing_device):
            self._update_enabled();return
        self._rendered_devices_signature=signature
        wanted={str(d['id']):d for d in visible_devices} if self.service.signed_in else {}
        previous_devices=self._devices
        reusable={key:row for key,row in self._rows_by_id.items() if key in wanted
            and getattr(row,'_current_device',None)==bool(wanted[key].get('current') or key==str(self.service.device_id))
            and previous_devices.get(key,{}).get('platform')==wanted[key].get('platform')}
        motion=None;positions={}
        from PySide6.QtWidgets import QScrollArea
        ancestor=self.devices_body.parentWidget()
        while ancestor is not None and not isinstance(ancestor,QScrollArea):ancestor=ancestor.parentWidget()
        topology=list(wanted)!=list(self._rows_by_id) or len(reusable)!=len(wanted)
        if ancestor is not None and topology:
            from item_transitions import ItemTransitions
            if not hasattr(self,'_device_motion'):self._device_motion=ItemTransitions(ancestor,self.devices_body)
            motion=self._device_motion;positions=motion.capture(self._rows_by_id)
            from ui_transitions import motion_allowed
            motion.remove((row for key,row in self._rows_by_id.items() if key not in wanted),
                          enabled=self.isVisible() and motion_allowed())
        for key,form in tuple(self._rename_forms.items()):
            if key not in reusable:form.hide();form.deleteLater()
        for field in ('device_buttons','device_rename_buttons','device_name_edits','_rename_forms','_rename_save_buttons'):
            setattr(self,field,{key:value for key,value in getattr(self,field).items() if key in reusable})
        if self._editing_device not in reusable:self._editing_device=''
        for row in self._device_rows:
            if row not in reusable.values():
                self.devices_layout.removeWidget(row);row.hide();row.deleteLater()
        self._devices={};self._rows_by_id={}
        self._device_rows=[]
        if self.service.signed_in:
            for device in devices:
                if not isinstance(device,dict) or not device.get('id') or device.get('revoked_at'):continue
                key=str(device['id']);self._devices[key]=dict(device)
                if key in reusable:
                    row=reusable[key];name=str(device.get('name') or '未命名设备')
                    name_label=row.findChild(AccountNameLabel,'accountDeviceName');name_label.setText(name)
                    platform={'windows':'Windows','macos':'Mac','linux':'Linux'}.get(str(device.get('platform','')),'其他设备')
                    timestamp=QDateTime.fromString(str(device.get('last_seen_at') or ''),Qt.DateFormat.ISODate)
                    suffix='此设备' if row._current_device else '最近在线 '+timestamp.toLocalTime().toString('MM-dd HH:mm') if timestamp.isValid() else ''
                    name_label.setToolTip(name+'\n'+platform+(' · '+suffix if suffix else ''))
                    row.setAccessibleName(name+('，本机' if row._current_device else ''))
                    self.device_rename_buttons[key].setAccessibleName('修改设备名称 '+name)
                    if key in self.device_buttons:self.device_buttons[key].setAccessibleName('移除设备 '+name)
                    if self._editing_device!=key:self.device_name_edits[key].setText(name)
                    elif previous_devices.get(key,{}).get('name')!=device.get('name'):self._cancel_rename()
                    self._device_rows.append(row);self._rows_by_id[key]=row
                    continue
                row=QFrame(self.devices_body);row.setObjectName('accountDevice')
                row.setMinimumHeight(44)
                row_layout=QVBoxLayout(row);row_layout.setContentsMargins(10,5,10,5);row_layout.setSpacing(8)
                horizontal=QHBoxLayout();horizontal.setSpacing(5);row_layout.addLayout(horizontal)
                glyph=AccountGlyph(row,device=True,platform=str(device.get('platform','')))
                glyph.setFixedSize(24,24)
                glyph.dark=self._dark;horizontal.addWidget(glyph)
                device_name=AccountNameLabel(row)
                device_name.setText(str(device.get('name') or '未命名设备'))
                device_name.setFixedHeight(24)
                device_name.setObjectName('accountDeviceName')
                device_name.setProperty('compactDeviceName',True)
                device_name.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
                device_name.setMaximumWidth(16777215)
                horizontal.addWidget(device_name,1)
                platform={'windows':'Windows','macos':'Mac','linux':'Linux'}.get(str(device.get('platform','')),'其他设备')
                current=bool(device.get('current') or key==str(self.service.device_id))
                row._current_device=current
                timestamp=QDateTime.fromString(str(device.get('last_seen_at') or ''),Qt.DateFormat.ISODate)
                suffix='此设备' if current else '最近在线 '+timestamp.toLocalTime().toString('MM-dd HH:mm') if timestamp.isValid() else ''
                device_name.setToolTip(str(device.get('name') or '未命名设备')+'\n'+platform+(' · '+suffix if suffix else ''))
                row.setAccessibleName(str(device.get('name') or '未命名设备')+'，'+platform+('，本机' if current else ''))
                subtitle=AccountNameLabel(row);subtitle.setObjectName('accountCurrentDevice' if current else 'accountDeviceMeta')
                subtitle.setFixedHeight(20)
                subtitle.setText('(本机)' if current else platform)
                if current:
                    subtitle.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Fixed)
                    subtitle.setFixedWidth(48)
                    horizontal.addWidget(subtitle)
                else:subtitle.hide()
                actions=QWidget(row);actions.setObjectName('accountDeviceActions')
                action_layout=QHBoxLayout(actions);action_layout.setContentsMargins(0,0,0,0);action_layout.setSpacing(3)
                actions.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Fixed)
                rename=RemoveDeviceButton(actions,rename=True);rename.dark=self._dark
                rename.setObjectName('accountDeviceRename');rename.setFixedSize(32,32)
                rename.setAccessibleName('修改设备名称 '+str(device.get('name') or '未命名设备'))
                rename.clicked.connect(partial(self._edit_device_name,key));rename.installEventFilter(self)
                self.device_rename_buttons[key]=rename;action_layout.addWidget(rename)
                if not current:
                    button=RemoveDeviceButton(actions);button.dark=self._dark
                    button.setObjectName('accountDeviceRevoke');button.setFixedSize(32,32)
                    button.setAccessibleName('移除设备 '+str(device.get('name') or '未命名设备'))
                    button.clicked.connect(partial(self._ask_remove,key));button.installEventFilter(self)
                    self.device_buttons[key]=button;action_layout.addWidget(button)
                    row.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                    def show_device_menu(point,key=key,row=row):
                        menu=QMenu(row);self._style_menu(menu);menu.addAction('修改设备名称',partial(self._edit_device_name,key));menu.exec(row.mapToGlobal(point))
                    row.customContextMenuRequested.connect(show_device_menu)
                if not current:action_layout.insertSpacing(0,18)
                actions.setFixedWidth(85 if not current else 32)
                horizontal.addWidget(actions,0,Qt.AlignmentFlag.AlignRight)
                from app_settings import ReferenceModalDialog
                rename_form=ReferenceModalDialog(self,surface_width=340,modal=True)
                rename_form.setObjectName('accountModal');rename_form.setWindowTitle('修改设备名称')
                rename_form.set_surface_theme('dark' if self._dark else 'light');rename_form.setStyleSheet(self.styleSheet())
                rename_form.body_layout.addWidget(_label('修改设备名称',rename_form.body))
                rename_layout=QHBoxLayout();rename_layout.setSpacing(6)
                edit=QLineEdit(rename_form.body);edit.setText(str(device.get('name') or ''))
                edit.setMaxLength(80);edit.setMinimumWidth(0);edit.setMinimumHeight(34)
                edit.setAccessibleName('新的设备名称')
                cancel=_button('取消',rename_form)
                save=_button('保存',rename_form,name='accountPrimary')
                cancel.clicked.connect(self._cancel_rename)
                save.clicked.connect(partial(self._save_device_name,key))
                edit.textChanged.connect(lambda *_:self._update_enabled())
                rename_form.body_layout.addWidget(edit)
                rename_layout.addStretch();rename_layout.addWidget(cancel);rename_layout.addWidget(save)
                rename_form.body_layout.addLayout(rename_layout);rename_form.hide()
                rename_form.rejected.connect(partial(self._rename_dismissed,key))
                edit.returnPressed.connect(save.click)
                self.device_name_edits[key]=edit;self._rename_forms[key]=rename_form
                self._rename_save_buttons[key]=save
                for child in rename_form.findChildren(QWidget):child.installEventFilter(self)
                self._device_rows.append(row)
                self._rows_by_id[key]=row
        if not self._device_rows:
            empty=_label('正在读取设备…' if self.service.signed_in else '',self.devices_body,muted=True)
            self.devices_layout.addWidget(empty,0,0,1,2);self._device_rows.append(empty)
        self.devices_heading_label.setText('已登录设备'+(' · '+str(len(self._devices)) if self._devices else ''))
        self._layout_device_rows()
        if motion is not None:
            self._fit_signed_content();self.devices_layout.activate()
            motion.reflow(self._rows_by_id,positions,enabled=self.isVisible() and motion_allowed())
        self.devices_body.updateGeometry();self.signed_in_page.updateGeometry();self.updateGeometry()

    def _layout_device_rows(self):
        columns=2 if self.devices_body.width()>=472 else 1
        signature=(columns,tuple(id(row) for row in self._device_rows),self.confirm_frame.isVisible())
        if getattr(self,'_device_layout_signature',None)==signature:return
        self._device_layout_signature=signature
        for index,row in enumerate(self._device_rows):
            self.devices_layout.removeWidget(row)
            self.devices_layout.addWidget(row,index//columns,index%columns,1,1 if len(self._rows_by_id) else columns)
        if self.confirm_frame.isVisible():
            self.devices_layout.removeWidget(self.confirm_frame)
            self.devices_layout.addWidget(self.confirm_frame,(len(self._device_rows)+columns-1)//columns,0,1,columns)
        self.devices_layout.setColumnStretch(0,1)
        self.devices_layout.setColumnStretch(1,1 if columns==2 else 0)
        self.devices_layout.invalidate()
        self.devices_body.updateGeometry();self.signed_in_page.updateGeometry()
        self.layout().invalidate();self.updateGeometry()
        self._update_enabled()
        self._queue_fit()

    def _queue_fit(self):
        if not self._fit_timer.isActive():self._fit_timer.start(0)

    def _fit_signed_content(self):
        if self.signed_in_page.isHidden():
            self.height_changed.emit();return
        width=max(1,self.signed_in_page.width())
        # Let the profile layout measure its wrapped subtitle at this width.
        # A changing fixed height became part of QWidgetItemV2's cached HFW,
        # leaving a stale 44px allocation for a subsequently 60px profile.
        devices=max(0,self.devices_layout.minimumSize().height()+2)
        self.devices_body.setFixedHeight(devices)
        # Layout owns child positions on every platform. Manual setGeometry
        # raced Cocoa's next layout pass and placed status text over controls.
        signed=self.signed_in_page.layout();signed.invalidate()
        self.signed_in_page.setFixedHeight(max(signed.minimumSize().height(),signed.sizeHint().height(),signed.totalHeightForWidth(width)))
        signed.activate()
        self.layout().invalidate();self.layout().activate();self.updateGeometry()
        self.height_changed.emit()

    def _edit_device_name(self,key,*_):
        if self._busy or not self.service.signed_in or key not in self._devices:return
        self._cancel_rename();self._cancel_remove();self._editing_device=key
        self.device_name_edits[key].setText(str(self._devices[key].get('name') or ''))
        self._rename_forms[key].show();self.device_name_edits[key].setFocus();self.device_name_edits[key].selectAll()
        self._fit_device_forms()

    def _cancel_rename(self,*_):
        from ui_transitions import fade_hide
        for form in self._rename_forms.values():fade_hide(form)
        self._editing_device=''
        self._fit_device_forms()

    def _fit_device_forms(self):
        self._update_enabled()

    def _rename_dismissed(self,key,*_):
        if self._editing_device==key:self._editing_device=''
        self._update_enabled()

    def _save_device_name(self,key,*_):
        if self._busy or not self.service.signed_in or key not in self._devices:return
        name=self.device_name_edits[key].text().strip()
        if not name:
            self._status_changed('请输入设备名称。');self.device_name_edits[key].setFocus();return
        self._invoke(self.service.rename_device,key,name)

    def _ask_remove(self,key,*_):
        device=self._devices.get(key)
        if (not device or self._busy or self._revoke_pending or device.get('current')
                or key==str(self.service.device_id) or not self.service.signed_in):return
        self._confirm_device=key
        self._cancel_rename()
        self.confirm_label.setText('移除“'+str(device.get('name') or '未命名设备')+'”的登录？此设备之后需要重新登录。')
        self.confirm_frame.show()
        self._layout_device_rows()

    def _cancel_remove(self):
        self._confirm_device='';self.confirm_label.clear();self.confirm_frame.hide()

    def _confirm_remove(self):
        key=self._confirm_device;device=self._devices.get(key)
        if not key or not device or self._busy or device.get('current') or key==str(self.service.device_id):return
        self._revoke_pending=key;self._update_enabled();self._invoke(self.service.revoke_device,key)

    def eventFilter(self,watched,event):
        if watched in (self.auth_page,self.password_form) and event.type()==QEvent.Resize:
            dialog=self.auth_dialog if watched is self.auth_page else self.password_dialog
            if dialog.isVisible():QTimer.singleShot(0,lambda dialog=dialog:self._fit_auth_form(dialog))
        if watched is self.devices_body and event.type()==QEvent.Type.Resize:
            self._layout_device_rows()
        if watched in (self.devices_body,self.profile_frame,self.signed_in_page) and event.type() in (QEvent.Type.LayoutRequest,QEvent.Type.Resize):
            self._queue_fit()
        # Enter in these embedded forms must not activate Settings' default Save.
        if event.type()==QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter):
            if watched is self.username_edit:self.password_edit.setFocus()
            elif watched is self.password_edit:self._submit_auth()
            elif watched is self.old_password_edit:self.new_password_edit.setFocus()
            elif watched is self.new_password_edit:self.confirm_password_edit.setFocus()
            elif watched is self.confirm_password_edit:self._change_password()
            elif watched in self.device_name_edits.values():
                key=next(key for key,edit in self.device_name_edits.items() if edit is watched)
                self._save_device_name(key)
            elif isinstance(watched,QPushButton):watched.click()
            else:return super().eventFilter(watched,event)
            event.accept();return True
        return super().eventFilter(watched,event)

    def hideEvent(self,event):
        self.password_edit.clear()
        self.close_auth()
        self._reset_password();self._cancel_rename()
        super().hideEvent(event)

    def apply_theme(self,theme):
        if getattr(self,'_applied_theme',None)==theme:return
        self._applied_theme=theme
        self._dark=theme=='dark'
        card,field,ink,secondary,edge,hover=(('#242426','#1c1c1e','#f5f5f7','#a1a1aa','#3a3a3c','#303034') if self._dark
            else ('#ffffff','#f8f9fb','#1d1d1f','#515154','#dfe2e8','#e8f2ff'))
        inset='#1c1c1e' if self._dark else '#f0f2f6'
        if sys.platform=='darwin':
            card='rgba(39,44,53,246)' if self._dark else 'rgba(255,255,255,248)'
            inset='rgba(32,37,46,230)' if self._dark else 'rgba(247,249,252,230)'
        success='#7dd6a8' if self._dark else '#14784a'
        style=f'''
            QFrame#accountPanel {{background:{card};border:1px solid {edge};border-radius:13px;}}
            QFrame#accountPanel QLabel {{background:transparent;color:{ink};font-size:13px;border:0;}}
            QFrame#accountPanel QLabel#accountTitle {{font-size:15px;font-weight:600;}}
            QFrame#accountPanel QLabel#accountSection {{font-size:14px;font-weight:600;}}
            QFrame#accountPanel QLabel#accountDeviceName {{font-size:14px;font-weight:500;}}
            QFrame#accountPanel QLabel#accountState {{color:{secondary};font-size:12px;background:{inset};border-radius:9px;padding:4px 9px;}}
            QFrame#accountPanel QLabel#accountState[online="true"] {{color:{success};background:{'#263c35' if self._dark else '#eaf7ef'};}}
            QFrame#accountPanel QLabel#accountUsername {{font-size:16px;font-weight:600;}}
            QFrame#accountPanel QLabel#accountMuted {{color:{secondary};font-size:13px;}}
            QFrame#accountPanel QLabel#accountSyncStatus {{color:{secondary};font-size:12px;padding:1px 2px;}}
            QFrame#accountProfile {{background:{inset};border:1px solid {edge};border-radius:11px;}}
            QFrame#accountActions, QFrame#accountDevices {{background:{inset};border:1px solid {edge};border-radius:10px;}}
            QFrame#accountActionSeparator, QFrame#accountDeviceSeparator {{background:{edge};border:0;}}
            QFrame#accountPanel QLineEdit {{background:{field};color:{ink};border:1px solid {edge};border-radius:8px;padding:7px 10px;font-size:14px;}}
            QFrame#accountPanel QLineEdit:focus {{border-color:#5d9eeb;}}
            QFrame#accountPanel QPushButton {{background:{card};color:{ink};border:1px solid {edge};border-radius:8px;padding:5px 12px;font-size:13px;}}
            QFrame#accountPanel QPushButton:hover {{background:{hover};border-color:#75a4dc;}}
            QFrame#accountPanel QPushButton:pressed {{background:{edge};}}
            QFrame#accountPanel QPushButton:focus {{border-color:#5d9eeb;}}
            QFrame#accountPanel QPushButton:disabled {{color:{secondary};border-color:{edge};}}
            QFrame#accountPanel QPushButton#accountPrimary {{background:#2584ed;color:white;border-color:#2584ed;}}
            QFrame#accountPanel QPushButton#accountPrimary:hover {{background:#3694fa;}}
            QFrame#accountPanel QPushButton#accountPrimary:disabled {{background:{edge};color:{secondary};border-color:{edge};}}
            QFrame#accountPanel QPushButton#accountQuiet {{background:transparent;color:{secondary};border:0;padding:5px 4px;}}
            QFrame#accountPanel QPushButton#accountQuiet:hover {{background:{hover};color:{ink};}}
            QFrame#accountPanel QPushButton#accountTab {{padding:5px 18px;}}
            QFrame#accountPanel QPushButton#accountTab:checked {{color:{ink};background:{hover};border-color:#75a4dc;}}
            QFrame#accountDevice {{background:transparent;border:0;}}
            QFrame#accountDevice:hover {{background:{hover};}}
            QFrame#accountPanel QPushButton#accountRemoveDevice {{background:transparent;border:0;padding:0;}}
            QFrame#accountPanel QPushButton#accountRemoveDevice:hover {{background:{hover};}}
            QFrame#accountConfirm {{background:{field};border:1px solid {edge};border-radius:9px;}}
            QFrame#accountSecurity {{background:{field};border:1px solid {edge};border-radius:9px;}}
            QFrame#accountPanel QPushButton#accountDanger {{color:{'#ffb1b5' if self._dark else '#b9404c'};}}
        '''
        card='rgba(255,255,255,10)' if self._dark else 'rgba(255,255,255,184)'
        edge='rgba(255,255,255,18)' if self._dark else 'rgba(0,0,0,18)'
        control='rgba(255,255,255,26)' if self._dark else 'rgba(0,0,0,14)'
        radius=12 if sys.platform=='darwin' else 8
        style+=f'''
            QFrame#accountPanel {{background:{card};border:1px solid {edge};border-radius:16px;}}
            QFrame#accountPanel QLabel {{font-size:12px;}}
            QFrame#accountPanel QLabel#accountUsername {{font-size:14px;font-weight:600;}}
            QFrame#accountPanel QLabel#accountState {{font-size:11px;padding:2px 8px;border-radius:10px;}}
            QFrame#accountPanel QLabel#accountState[online="true"] {{color:#30D158;background:rgba(48,209,88,51);}}
            QFrame#accountPanel QLabel#accountDeviceName {{font-size:12px;font-weight:500;}}
            QFrame#accountPanel QLabel#accountCurrentDevice {{font-size:11px;color:#0A84FF;}}
            QFrame#accountPanel QLabel#accountSyncStatus {{font-size:12px;color:{secondary};padding:0;}}
            QFrame#accountProfile,QFrame#accountDevices {{background:transparent;border:0;}}
            QFrame#accountDevices {{border-top:1px solid {edge};}}
            QFrame#accountDevice {{background:{'rgba(255,255,255,8)' if self._dark else 'rgba(0,0,0,6)'};border:1px solid {edge};border-radius:12px;}}
            QFrame#accountPanel QPushButton {{background:{control};color:{ink};border:0;padding:5px 12px;font-size:12px;}}
            QFrame#accountPanel QPushButton#accountDanger {{background:rgba(255,69,58,38);color:#FF453A;}}
            QFrame#accountPanel QPushButton#accountQuiet {{background:{control};color:{ink};border:0;padding:5px 12px;}}
            QFrame#accountPanel QPushButton#accountUnsignedProfile {{background:transparent;border:0;padding:0;text-align:left;}}
            QFrame#accountPanel QPushButton#accountUnsignedProfile:hover {{background:transparent;}}
            QFrame#accountPanel QLabel#accountUnsignedTitle {{font-size:14px;font-weight:600;}}
            QFrame#accountPanel QPushButton#accountDeviceRename {{background:transparent;border:0;padding:0;color:#0A84FF;font-size:12px;}}
            QFrame#accountPanel QPushButton#accountDeviceRevoke {{background:transparent;border:0;padding:0;color:#FF453A;font-size:12px;}}
            QFrame#accountPanel QPushButton#accountDeviceRename:hover {{background:rgba(10,132,255,20);border-radius:7px;}}
            QFrame#accountPanel QPushButton#accountDeviceRevoke:hover {{background:rgba(255,69,58,20);border-radius:7px;}}
            QFrame#accountPanel QPushButton#accountPrimary {{background:#0A84FF;color:#FFFFFF;border:0;}}
            QFrame#accountAuthTabs {{background:{'rgba(0,0,0,64)' if self._dark else 'rgba(240,242,247,191)'};border:1px solid {edge};border-radius:12px;}}
            QFrame#accountPanel QPushButton#accountTab {{background:transparent;border:0;padding:4px 6px;border-radius:8px;}}
            QFrame#accountPanel QPushButton#accountTab:checked {{background:{'rgba(255,255,255,38)' if self._dark else '#FFFFFF'};color:{ink};}}
            QFrame#accountPanel QLineEdit {{background:{'#242426' if self._dark else '#FFFFFF'};font-size:12px;padding:8px 12px;border:1px solid {edge};border-radius:12px;}}
            QDialog#accountModal,QDialog#accountModal QFrame#referenceModalSurface {{background:transparent;border:0;}}
            QDialog#accountModal QFrame#accountModalHeader {{background:transparent;border:0;border-bottom:1px solid {edge};}}
            QDialog#accountModal QLabel {{color:{ink};font-size:12px;background:transparent;}}
            QDialog#accountModal QLabel#accountModalTitle {{font-size:14px;font-weight:600;}}
            QDialog#accountModal QLineEdit {{background:{'#242426' if self._dark else '#FFFFFF'};color:{ink};border:1px solid {edge};border-radius:12px;padding:8px 12px;font-size:12px;}}
            QDialog#accountModal QPushButton {{background:{control};color:{ink};border:0;border-radius:12px;padding:6px 16px;font-size:12px;}}
            QDialog#accountModal QPushButton#accountPrimary {{background:#0A84FF;color:#FFFFFF;}}
            QDialog#accountModal QPushButton#accountModalClose {{background:transparent;padding:0;border-radius:8px;}}
            QDialog#accountModal QPushButton#accountTab {{background:transparent;border:0;padding:4px 6px;border-radius:8px;}}
            QDialog#accountModal QPushButton#accountTab:checked {{background:{'rgba(255,255,255,38)' if self._dark else '#FFFFFF'};color:{ink};}}
            QFrame#accountSecurity {{background:transparent;border:0;}}
        '''
        for dialog in (self.password_dialog,self.auth_dialog,*self._rename_forms.values()):
            dialog.set_surface_theme(theme)
        for edit in (self.password_edit,self.old_password_edit,self.new_password_edit,self.confirm_password_edit):
            edit.set_visibility_theme(theme)
        for button in (self.password_close,self.auth_close):button.dark=self._dark;button.update()
        semantic=ui_colors(theme)
        style+=f'''
            QFrame#accountPanel QLabel {{font-size:13px;}}
            QFrame#accountPanel QLabel#accountUsername {{font-size:16px;font-weight:600;}}
            QFrame#accountPanel QLabel#accountState[online="true"] {{color:{semantic['green']};}}
            QFrame#accountPanel QLabel#accountDeviceName {{font-size:13px;font-weight:500;}}
            QFrame#accountPanel QLabel#accountDeviceMeta {{font-size:11px;color:{semantic['secondary']};}}
            QFrame#accountPanel QLabel#accountCurrentDevice,QFrame#accountPanel QPushButton#accountDeviceRename {{color:{semantic['link']};}}
            QFrame#accountPanel QPushButton#accountDeviceRevoke,QFrame#accountPanel QPushButton#accountDanger {{color:{semantic['red']};}}
            QFrame#accountPanel QPushButton#accountDeviceRevoke {{padding:0;text-align:right;}}
            QFrame#accountPanel QPushButton#accountPrimary,QDialog#accountModal QPushButton#accountPrimary {{background:{semantic['action']};color:{semantic['on_action']};}}
            QFrame#accountPanel QPushButton#accountPrimary:hover,QDialog#accountModal QPushButton#accountPrimary:hover {{background:{semantic['action_hover']};color:{semantic['on_action']};}}
            QFrame#accountPanel QPushButton:focus {{border-color:transparent;}}
            QFrame#accountPanel QPushButton[keyboardFocus="true"],QDialog#accountModal QPushButton[keyboardFocus="true"] {{border:1px solid {semantic['link']};}}
            QFrame#accountPanel QPushButton:disabled,QDialog#accountModal QPushButton:disabled {{color:{semantic['muted']};background:{semantic['control']};}}
            QFrame#accountPanel QPushButton#accountUnsignedProfile {{background:{semantic['card']};border:1px solid {semantic['border']};border-radius:12px;padding:0;}}
            QFrame#accountPanel QPushButton#accountUnsignedProfile:hover {{background:{semantic['hover']};}}
        '''
        for button in [*self.device_buttons.values(),*self.device_rename_buttons.values()]:button.dark=self._dark;button.update()
        from native_ui_common import readable_qss
        style=readable_qss(style)
        if sys.platform=='win32':
            style+='\nQFrame#accountPanel QLabel#accountState {font-size:12px;}'
        self.setStyleSheet(style)
        for form in self._rename_forms.values():form.setStyleSheet(style)
        for row in self._device_rows:
            for name in row.findChildren(AccountNameLabel):
                if name.property('compactDeviceName'):name.setMaximumWidth(16777215)
        for glyph in self.findChildren(AccountGlyph):glyph.dark=self._dark;glyph.update()

"""Inline account controls. Credentials remain only in the temporary form."""
from __future__ import annotations

from functools import partial
import re

from PySide6.QtCore import QByteArray, QDateTime, QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)


def _label(text='', parent=None, *, muted=False):
    label=QLabel(str(text),parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
    if muted:label.setObjectName('accountMuted')
    return label


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
        self.setToolTip('修改设备名称' if rename else '移除设备')

    def paintEvent(self,event):
        super().paintEvent(event)
        color='#c2cddd' if self.dark else '#68788f'
        path=('m11.5 3.5 3 3M3 15l3.4-.7L15 5.7a1.1 1.1 0 0 0 0-1.5l-1.2-1.3a1.1 1.1 0 0 0-1.6 0L3.7 11.5 3 15' if self.rename
              else 'M3 5h12M7 2.5h4l1 2.5M5 5l.6 10.5h6.8L13 5M7.5 7.5v5.5M10.5 7.5v5.5')
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><path d="{path}" fill="none" stroke="{color}" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        painter=QPainter(self)
        if not self.isEnabled():painter.setOpacity(.4)
        QSvgRenderer(QByteArray(svg.encode('utf8'))).render(painter,QRectF(9,9,14,14))


class AccountGlyph(QWidget):
    """A single vector family for the account and device identity markers."""
    def __init__(self,parent=None,*,device=False):
        super().__init__(parent)
        self.device=device;self.dark=False
        self.setFixedSize(34 if device else 44,34 if device else 44)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#344459' if self.dark else '#eaf2fd'))
        painter.drawRoundedRect(QRectF(self.rect()),10,10)
        path=('M4 4h16v11H4zM9 20h6M12 15v5' if self.device
              else 'M8 8a4 4 0 1 0 8 0 4 4 0 0 0-8 0M5 21v-2a7 7 0 0 1 14 0v2')
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{path}" fill="none" stroke="{"#8dc0ff" if self.dark else "#377cc7"}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        size=19 if self.device else 23
        QSvgRenderer(QByteArray(svg.encode())).render(painter,QRectF((self.width()-size)/2,(self.height()-size)/2,size,size))


class AccountPanel(QFrame):
    """AccountService presentation, embedded in SettingsDialog's account slot."""
    def __init__(self,service,parent=None,*,theme='light',email_binding=True):
        super().__init__(parent)
        self.service=service
        self.email_binding=bool(email_binding)
        self.setObjectName('accountPanel')
        self.setFont(QFont('HarmonyOS Sans SC',11))
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        self._mode='login';self._account_id='';self._busy=bool(service.busy)
        self._challenge='';self._challenge_email='';self._requested_email=''
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
        auth.addWidget(_label('使用同一账号，Windows 和 Mac 会自动同步内容。',self.auth_page,muted=True))
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
        self.password_edit=QLineEdit(self.auth_page);self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setAccessibleName('密码');self.password_edit.setMaxLength(256)
        auth.addWidget(self.password_edit)
        self.submit_button=_button('登录',self.auth_page,name='accountPrimary')
        self.submit_button.clicked.connect(self._submit_auth);auth.addWidget(self.submit_button)
        layout.addWidget(self.auth_page)

        self.signed_in_page=QWidget(self);signed=QVBoxLayout(self.signed_in_page)
        signed.setContentsMargins(0,0,0,0);signed.setSpacing(12)
        identity=QHBoxLayout();identity.setSpacing(10)
        self.account_glyph=AccountGlyph(self.signed_in_page)
        identity.addWidget(self.account_glyph)
        identity_text=QVBoxLayout();identity_text.setSpacing(3)
        self.username_label=_label('',self.signed_in_page);self.username_label.setObjectName('accountUsername')
        identity_text.addWidget(self.username_label)
        self.sync_status_label=_label('',self.signed_in_page,muted=True)
        self.sync_status_label.setAccessibleName('云同步状态');identity_text.addWidget(self.sync_status_label)
        identity.addLayout(identity_text,1)
        self.logout_button=_button('退出登录',self.signed_in_page,name='accountQuiet')
        self.logout_button.clicked.connect(lambda:self._invoke(self.service.logout));identity.addWidget(self.logout_button)
        signed.addLayout(identity)
        security_heading=QHBoxLayout();security_heading.setSpacing(8)
        security_heading.addWidget(_label('账号安全',self.signed_in_page),1)
        self.password_toggle_button=_button('修改密码',self.signed_in_page,name='accountQuiet')
        self.password_toggle_button.setCheckable(True)
        self.password_toggle_button.clicked.connect(self._toggle_password)
        security_heading.addWidget(self.password_toggle_button);signed.addLayout(security_heading)
        self.password_form=QFrame(self.signed_in_page);self.password_form.setObjectName('accountSecurity')
        password_layout=QVBoxLayout(self.password_form);password_layout.setContentsMargins(12,12,12,12);password_layout.setSpacing(8)
        for attribute,label,placeholder in (
                ('old_password_edit','原密码','输入当前密码'),
                ('new_password_edit','新密码','设置 10–128 位密码'),
                ('confirm_password_edit','确认新密码','再次输入新密码')):
            password_layout.addWidget(_label(label,self.password_form))
            edit=QLineEdit(self.password_form);edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setAccessibleName(label);edit.setPlaceholderText(placeholder);edit.setMaxLength(256)
            setattr(self,attribute,edit);password_layout.addWidget(edit)
        password_layout.addWidget(_label('修改后，其他设备需要重新登录。',self.password_form,muted=True))
        password_actions=QHBoxLayout();password_actions.addStretch()
        self.password_cancel_button=_button('取消',self.password_form)
        self.password_change_button=_button('更新密码',self.password_form,name='accountPrimary')
        self.password_cancel_button.clicked.connect(self._reset_password)
        self.password_change_button.clicked.connect(self._change_password)
        password_actions.addWidget(self.password_cancel_button);password_actions.addWidget(self.password_change_button)
        password_layout.addLayout(password_actions);signed.addWidget(self.password_form);self.password_form.hide()
        email_row=QHBoxLayout();email_row.setSpacing(8)
        self.email_status_label=_label('',self.signed_in_page,muted=True);email_row.addWidget(self.email_status_label,1)
        self.email_toggle_button=_button('绑定邮箱',self.signed_in_page,name='accountQuiet')
        self.email_toggle_button.clicked.connect(self._toggle_email);email_row.addWidget(self.email_toggle_button)
        signed.addLayout(email_row)
        self.email_form=QWidget(self.signed_in_page);email=QVBoxLayout(self.email_form)
        email.setContentsMargins(0,0,0,0);email.setSpacing(8)
        email.addWidget(_label('邮箱绑定是可选的，不影响登录和同步。',self.email_form,muted=True))
        email_input=QHBoxLayout();email_input.setSpacing(8)
        self.email_edit=QLineEdit(self.email_form);self.email_edit.setPlaceholderText('邮箱地址')
        self.email_edit.setAccessibleName('绑定邮箱地址');self.email_edit.setMaxLength(254)
        self.email_edit.textChanged.connect(self._email_changed);email_input.addWidget(self.email_edit,1)
        self.send_code_button=_button('发送验证码',self.email_form)
        self.send_code_button.clicked.connect(self._send_email_code);email_input.addWidget(self.send_code_button)
        email.addLayout(email_input)
        code_row=QHBoxLayout();code_row.setSpacing(8)
        self.code_edit=QLineEdit(self.email_form);self.code_edit.setPlaceholderText('6 位邮箱验证码')
        self.code_edit.setAccessibleName('邮箱验证码');self.code_edit.setMaxLength(6)
        code_row.addWidget(self.code_edit,1)
        self.bind_button=_button('绑定邮箱',self.email_form,name='accountPrimary')
        self.bind_button.clicked.connect(self._bind_email);code_row.addWidget(self.bind_button)
        email.addLayout(code_row);signed.addWidget(self.email_form)
        devices_header=QHBoxLayout()
        self.devices_heading_label=_label('已登录设备',self.signed_in_page)
        devices_header.addWidget(self.devices_heading_label,1)
        self.refresh_button=_button('刷新',self.signed_in_page,name='accountQuiet')
        self.refresh_button.clicked.connect(self.refresh_devices);devices_header.addWidget(self.refresh_button)
        signed.addLayout(devices_header)
        self.devices_body=QWidget(self.signed_in_page);self.devices_layout=QVBoxLayout(self.devices_body)
        self.devices_layout.setContentsMargins(0,0,0,0);self.devices_layout.setSpacing(7)
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
        for field in self.findChildren(QLineEdit):
            field.setMinimumWidth(0);field.setMinimumHeight(34)
        for widget in self.findChildren(QWidget):widget.installEventFilter(self)
        service.account_changed.connect(self._account_changed)
        service.status_changed.connect(self._status_changed)
        service.busy_changed.connect(self._busy_changed)
        service.devices_changed.connect(self._devices_changed)
        service.email_challenge.connect(self._email_challenge)
        if hasattr(service,'password_changed'):service.password_changed.connect(self._password_changed)
        self.set_auth_mode('login');self.apply_theme(theme)
        self._account_changed(service.account)
        self.email_status_label.setVisible(self.email_binding)
        self.email_toggle_button.setVisible(self.email_binding)
        if not self.email_binding:self.email_form.hide()
        if service.signed_in:self.refresh_devices()

    def set_auth_mode(self,mode):
        if mode not in ('login','register'):raise ValueError('Unknown account form mode')
        self._mode=mode
        self.login_tab.setChecked(mode=='login');self.register_tab.setChecked(mode=='register')
        self.submit_button.setText('注册' if mode=='register' else '登录')
        self.password_edit.setPlaceholderText('设置 10–128 位密码' if mode=='register' else '请输入密码')
        self.password_edit.clear();self.status_label.clear()

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
            self.password_edit.clear();self.username_edit.clear();self.status_label.clear()
            self.username_label.clear();self.email_status_label.clear();self.sync_status_label.clear()
            self._reset_password()
            self._reset_email();self.email_form.hide();self._refreshing=False;self._revoke_pending=''
            self._devices_changed([])
            if signed:self.sync_status_label.setText('正在准备同步…')
        self.auth_page.setVisible(not signed);self.signed_in_page.setVisible(signed)
        if signed:
            self.password_edit.clear()
            self.username_label.setText(str(account.get('username','')))
            address=account.get('email')
            verified=bool(address and account.get('email_verified_at'))
            self.email_status_label.setText(('邮箱：'+str(address)+('' if verified else '（待验证）')) if address else '邮箱未绑定（可选）')
            self.email_toggle_button.setText('更换邮箱' if address else '绑定邮箱')
            if verified and str(address).casefold()==self._challenge_email.casefold():
                self._reset_email();self.email_form.hide()
        self._update_enabled()

    def set_sync_status(self,text):
        if self.service.signed_in:self.sync_status_label.setText(str(text)[:240])

    def _status_changed(self,text):
        message=str(text)
        for edit in (self.password_edit,self.old_password_edit,self.new_password_edit,self.confirm_password_edit):
            secret=edit.text()
            if secret:message=message.replace(secret,'…')
        self.status_label.setText(message[:240])
        self._refreshing=False;self._revoke_pending='';self._cancel_remove();self._update_enabled()

    def _busy_changed(self,busy):
        self._busy=bool(busy)
        if not self._busy:self.password_edit.clear()
        self._update_enabled()

    def _update_enabled(self):
        signed=bool(self.service.signed_in)
        self.account_state_label.setText('处理中…' if self._busy else '已登录' if signed else '未登录')
        for widget in (self.username_edit,self.password_edit,self.submit_button,self.login_tab,self.register_tab):
            widget.setEnabled(not self._busy)
        for widget in (self.logout_button,self.email_toggle_button,self.email_edit,self.send_code_button):
            widget.setEnabled(signed and not self._busy)
        for widget in (self.password_toggle_button,self.old_password_edit,self.new_password_edit,
                       self.confirm_password_edit,self.password_change_button,self.password_cancel_button):
            widget.setEnabled(signed and not self._busy)
        valid=bool(self._challenge and self.email_edit.text().strip().casefold()==self._challenge_email.casefold())
        self.code_edit.setEnabled(signed and not self._busy and valid)
        self.bind_button.setEnabled(signed and not self._busy and valid)
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
        if checked:self.old_password_edit.setFocus()
        else:self._reset_password()

    def _reset_password(self):
        for edit in (self.old_password_edit,self.new_password_edit,self.confirm_password_edit):edit.clear()
        self.password_toggle_button.setChecked(False);self.password_form.hide()

    def _change_password(self):
        if self._busy or not self.service.signed_in:return
        old,new,confirm=self.old_password_edit.text(),self.new_password_edit.text(),self.confirm_password_edit.text()
        if not old:
            self._status_changed('请输入原密码。');self.old_password_edit.setFocus();return
        if not 10<=len(new)<=128:
            self._status_changed('新密码需要 10–128 个字符。');self.new_password_edit.setFocus();return
        if old==new:
            self._status_changed('新密码需要与原密码不同。');self.new_password_edit.setFocus();return
        if new!=confirm:
            self._status_changed('两次输入的新密码不一致。');self.confirm_password_edit.setFocus();return
        self._invoke(self.service.change_password,old,new,confirm)

    def _password_changed(self):
        self._reset_password()
        self._status_changed('密码已更新，其他设备需要重新登录。')

    def _toggle_email(self):
        self.email_form.setVisible(not self.email_form.isVisible())
        if self.email_form.isVisible():self.email_edit.setFocus()

    def _reset_email(self):
        self._challenge=self._challenge_email=self._requested_email=''
        self.email_edit.clear();self.code_edit.clear()

    def _email_changed(self,*_):
        self._challenge=self._challenge_email=self._requested_email='';self.code_edit.clear()
        self._update_enabled()

    def _send_email_code(self):
        if not self.service.signed_in or self._busy:return
        email=self.email_edit.text().strip()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
            self._status_changed('请输入有效的邮箱地址。');return
        self._challenge=self._challenge_email='';self.code_edit.clear();self._requested_email=email
        self._update_enabled();self._invoke(self.service.request_email_code,email)

    def _email_challenge(self,challenge,email):
        if (not self.service.signed_in or not challenge or not self._requested_email
                or email.casefold()!=self._requested_email.casefold()
                or email.casefold()!=self.email_edit.text().strip().casefold()):return
        self._challenge,self._challenge_email=str(challenge),str(email)
        self._update_enabled();self.code_edit.setFocus()

    def _bind_email(self):
        if self._busy or not self.bind_button.isEnabled():return
        if not re.fullmatch(r'[0-9]{6}',self.code_edit.text().strip()):
            self._status_changed('请输入 6 位邮箱验证码。');return
        self._invoke(self.service.bind_email,self._challenge,self.code_edit.text().strip())

    def refresh_devices(self):
        if not self.service.signed_in or self._busy or self._refreshing:return
        self._refreshing=True;self._cancel_remove();self._update_enabled()
        self._invoke(self.service.list_devices)

    def _devices_changed(self,devices):
        self._refreshing=False;self._revoke_pending='';self._cancel_remove()
        self._devices={};self.device_buttons={};self._rows_by_id={}
        self.device_rename_buttons={};self.device_name_edits={};self._rename_forms={}
        self._rename_save_buttons={};self._editing_device=''
        for row in self._device_rows:
            self.devices_layout.removeWidget(row)
            for label in row.findChildren(QLabel):label.clear()
            row.hide();row.deleteLater()
        self._device_rows=[]
        if self.service.signed_in:
            for device in devices:
                if not isinstance(device,dict) or not device.get('id') or device.get('revoked_at'):continue
                key=str(device['id']);self._devices[key]=dict(device)
                row=QFrame(self.devices_body);row.setObjectName('accountDevice')
                row_layout=QVBoxLayout(row);row_layout.setContentsMargins(10,10,8,10);row_layout.setSpacing(8)
                horizontal=QHBoxLayout();horizontal.setSpacing(9);row_layout.addLayout(horizontal)
                glyph=AccountGlyph(row,device=True);glyph.dark=self._dark;horizontal.addWidget(glyph)
                detail=QVBoxLayout();detail.setSpacing(3)
                detail.addWidget(_label(str(device.get('name') or '未命名设备'),row))
                platform={'windows':'Windows','macos':'Mac','linux':'Linux'}.get(str(device.get('platform','')),'其他设备')
                current=bool(device.get('current') or key==str(self.service.device_id))
                timestamp=QDateTime.fromString(str(device.get('last_seen_at') or ''),Qt.DateFormat.ISODate)
                suffix='此设备' if current else '最近在线 '+timestamp.toLocalTime().toString('MM-dd HH:mm') if timestamp.isValid() else ''
                detail.addWidget(_label(platform+(' · '+suffix if suffix else ''),row,muted=True))
                horizontal.addLayout(detail,1)
                rename=RemoveDeviceButton(row,rename=True);rename.dark=self._dark
                rename.setAccessibleName('修改设备名称 '+str(device.get('name') or '未命名设备'))
                rename.clicked.connect(partial(self._edit_device_name,key));rename.installEventFilter(self)
                self.device_rename_buttons[key]=rename;horizontal.addWidget(rename)
                if not current:
                    button=RemoveDeviceButton(row);button.dark=self._dark
                    button.setAccessibleName('移除设备 '+str(device.get('name') or '未命名设备'))
                    button.clicked.connect(partial(self._ask_remove,key));button.installEventFilter(self)
                    self.device_buttons[key]=button;horizontal.addWidget(button)
                rename_form=QWidget(row);rename_layout=QHBoxLayout(rename_form)
                rename_layout.setContentsMargins(0,0,0,0);rename_layout.setSpacing(6)
                edit=QLineEdit(rename_form);edit.setText(str(device.get('name') or ''))
                edit.setMaxLength(80);edit.setMinimumWidth(0);edit.setMinimumHeight(34)
                edit.setAccessibleName('新的设备名称')
                cancel=_button('取消',rename_form)
                save=_button('保存',rename_form,name='accountPrimary')
                cancel.clicked.connect(self._cancel_rename)
                save.clicked.connect(partial(self._save_device_name,key))
                edit.textChanged.connect(lambda *_:self._update_enabled())
                rename_layout.addWidget(edit,1);rename_layout.addWidget(cancel);rename_layout.addWidget(save)
                row_layout.addWidget(rename_form);rename_form.hide()
                self.device_name_edits[key]=edit;self._rename_forms[key]=rename_form
                self._rename_save_buttons[key]=save
                for child in rename_form.findChildren(QWidget):child.installEventFilter(self)
                self.devices_layout.addWidget(row);self._device_rows.append(row)
                self._rows_by_id[key]=row
        if not self._device_rows:
            empty=_label('正在读取设备…' if self.service.signed_in else '',self.devices_body,muted=True)
            self.devices_layout.addWidget(empty);self._device_rows.append(empty)
        self.devices_heading_label.setText('已登录设备'+(' · '+str(len(self._devices)) if self._devices else ''))
        self._update_enabled()

    def _edit_device_name(self,key,*_):
        if self._busy or not self.service.signed_in or key not in self._devices:return
        self._cancel_rename();self._cancel_remove();self._editing_device=key
        self.device_name_edits[key].setText(str(self._devices[key].get('name') or ''))
        self._rename_forms[key].show();self.device_name_edits[key].setFocus();self.device_name_edits[key].selectAll()

    def _cancel_rename(self,*_):
        for form in self._rename_forms.values():form.hide()
        self._editing_device=''

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
        self.devices_layout.insertWidget(self.devices_layout.indexOf(self._rows_by_id[key])+1,self.confirm_frame)
        self.confirm_frame.show()

    def _cancel_remove(self):
        self._confirm_device='';self.confirm_label.clear();self.confirm_frame.hide()

    def _confirm_remove(self):
        key=self._confirm_device;device=self._devices.get(key)
        if not key or not device or self._busy or device.get('current') or key==str(self.service.device_id):return
        self._revoke_pending=key;self._update_enabled();self._invoke(self.service.revoke_device,key)

    def eventFilter(self,watched,event):
        # Enter in these embedded forms must not activate Settings' default Save.
        if event.type()==QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter):
            if watched is self.username_edit:self.password_edit.setFocus()
            elif watched is self.password_edit:self._submit_auth()
            elif watched is self.email_edit:self._send_email_code()
            elif watched is self.code_edit:self._bind_email()
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
        self.password_edit.clear();self.code_edit.clear()
        self._reset_password();self._cancel_rename()
        super().hideEvent(event)

    def apply_theme(self,theme):
        self._dark=theme=='dark'
        card,field,ink,secondary,edge,hover=(('#272c35','#20252e','#edf2fa','#a5b1c3','#3c4554','#39465a') if self._dark
            else ('#ffffff','#fbfcfe','#253246','#7a8799','#dce3ed','#edf4fe'))
        self.setStyleSheet(f'''
            QFrame#accountPanel {{background:{card};border:1px solid {edge};border-radius:13px;}}
            QFrame#accountPanel QLabel {{background:transparent;color:{ink};font-size:13px;border:0;}}
            QFrame#accountPanel QLabel#accountTitle {{font-weight:600;}}
            QFrame#accountPanel QLabel#accountState {{color:{secondary};font-size:12px;background:{field};border-radius:9px;padding:4px 8px;}}
            QFrame#accountPanel QLabel#accountUsername {{font-size:16px;font-weight:600;}}
            QFrame#accountPanel QLabel#accountMuted {{color:{secondary};font-size:12px;}}
            QFrame#accountPanel QLineEdit {{background:{field};color:{ink};border:1px solid {edge};border-radius:8px;padding:7px 10px;font-size:13px;}}
            QFrame#accountPanel QLineEdit:focus {{border-color:#5d9eeb;}}
            QFrame#accountPanel QPushButton {{background:{card};color:{ink};border:1px solid {edge};border-radius:8px;padding:5px 12px;font-size:13px;}}
            QFrame#accountPanel QPushButton:hover {{background:{hover};border-color:#75a4dc;}}
            QFrame#accountPanel QPushButton:pressed {{background:{edge};}}
            QFrame#accountPanel QPushButton:focus {{border-color:#5d9eeb;}}
            QFrame#accountPanel QPushButton:disabled {{color:{secondary};border-color:{edge};}}
            QFrame#accountPanel QPushButton#accountPrimary {{background:#2584ed;color:white;border-color:#2584ed;}}
            QFrame#accountPanel QPushButton#accountPrimary:hover {{background:#3694fa;}}
            QFrame#accountPanel QPushButton#accountPrimary:disabled {{background:{edge};color:{secondary};border-color:{edge};}}
            QFrame#accountPanel QPushButton#accountQuiet {{background:transparent;color:{secondary};border:0;padding:5px 0;}}
            QFrame#accountPanel QPushButton#accountQuiet:hover {{color:{ink};}}
            QFrame#accountPanel QPushButton#accountTab {{padding:5px 18px;}}
            QFrame#accountPanel QPushButton#accountTab:checked {{color:{ink};background:{hover};border-color:#75a4dc;}}
            QFrame#accountDevice {{background:{field};border:1px solid {edge};border-radius:9px;}}
            QFrame#accountPanel QPushButton#accountRemoveDevice {{background:transparent;border:0;padding:0;}}
            QFrame#accountPanel QPushButton#accountRemoveDevice:hover {{background:{hover};}}
            QFrame#accountConfirm {{background:{field};border:1px solid {edge};border-radius:9px;}}
            QFrame#accountSecurity {{background:{field};border:1px solid {edge};border-radius:9px;}}
            QFrame#accountPanel QPushButton#accountDanger {{color:{'#ffb1b5' if self._dark else '#b9404c'};}}
        ''')
        for button in [*self.device_buttons.values(),*self.device_rename_buttons.values()]:button.dark=self._dark;button.update()
        for glyph in self.findChildren(AccountGlyph):glyph.dark=self._dark;glyph.update()

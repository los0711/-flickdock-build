"""Inline account controls. Credentials remain only in the temporary form."""
from __future__ import annotations

from functools import partial
import sys
from account_service import valid_new_password

from PySide6.QtCore import QByteArray, QDateTime, QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter
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
    def __init__(self,parent=None,*,device=False,platform=''):
        super().__init__(parent)
        self.device=device;self.platform=platform;self.dark=False
        self.setFixedSize(34 if device else 44,34 if device else 44)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#344459' if self.dark else '#eaf2fd'))
        painter.drawRoundedRect(QRectF(self.rect()),10,10)
        if self.device and self.platform=='windows':
            path='M4 5l7-1v7H4zM13 3.7L20 3v8h-7zM4 13h7v7l-7-1zM13 13h7v8l-7-1z'
        elif self.device:
            path='M4 4h16v11H4zM9 20h6M12 15v5'
        else:
            path='M8 8a4 4 0 1 0 8 0 4 4 0 0 0-8 0M5 21v-2a7 7 0 0 1 14 0v2'
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{path}" fill="none" stroke="{"#8dc0ff" if self.dark else "#377cc7"}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        size=19 if self.device else 23
        QSvgRenderer(QByteArray(svg.encode())).render(painter,QRectF((self.width()-size)/2,(self.height()-size)/2,size,size))


class AccountPanel(QFrame):
    """AccountService presentation, embedded in SettingsDialog's account slot."""
    def __init__(self,service,parent=None,*,theme='light',email_binding=False):
        super().__init__(parent)
        self.service=service
        # Keep the argument for older app builds; the email controls are gone.
        self.email_binding=False
        self.setObjectName('accountPanel')
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
                     if sys.platform=='darwin' else QFont('HarmonyOS Sans SC',11))
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
        self.password_edit=QLineEdit(self.auth_page);self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setAccessibleName('密码')
        auth.addWidget(self.password_edit)
        self.submit_button=_button('登录',self.auth_page,name='accountPrimary')
        self.submit_button.clicked.connect(self._submit_auth);auth.addWidget(self.submit_button)
        self.auth_status_label=_label('',self.auth_page,muted=True)
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
            edit=QLineEdit(self.password_form);edit.setEchoMode(QLineEdit.EchoMode.Password)
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

    def set_auth_mode(self,mode):
        if mode not in ('login','register'):raise ValueError('Unknown account form mode')
        self._mode=mode
        self.login_tab.setChecked(mode=='login');self.register_tab.setChecked(mode=='register')
        self.submit_button.setText('注册' if mode=='register' else '登录')
        self.password_edit.setPlaceholderText('至少 6 个字符，不含空白' if mode=='register' else '请输入密码')
        self.password_edit.clear();self.status_label.clear();self.auth_status_label.clear()

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
        self.auth_page.setVisible(not signed);self.signed_in_page.setVisible(signed)
        self.status_label.setVisible(signed)
        if signed:
            self.password_edit.clear()
            self.username_label.setText(str(account.get('username','')))
            alias=account.get('login_alias')
            self.login_alias_edit.setText(alias if isinstance(alias,str) else '')
            self.login_alias_edit.setVisible(bool(isinstance(alias,str) and alias))
        self._update_enabled()

    def set_sync_status(self,text):
        if self.service.signed_in:self.sync_status_label.setText(str(text)[:240])

    def _status_changed(self,text):
        message=str(text)
        for edit in (self.password_edit,self.old_password_edit,self.new_password_edit,self.confirm_password_edit):
            secret=edit.text()
            if secret:message=message.replace(secret,'…')
        self.status_label.setText(message[:240])
        self.auth_status_label.setText(message[:240])
        self._refreshing=False;self._revoke_pending='';self._cancel_remove();self._update_enabled()

    def _busy_changed(self,busy):
        self._busy=bool(busy)
        if not self._busy:self.password_edit.clear()
        self._update_enabled()

    def _update_enabled(self):
        signed=bool(self.service.signed_in)
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
                if self._device_rows:
                    separator=QFrame(self.devices_body)
                    separator.setObjectName('accountDeviceSeparator')
                    separator.setFixedHeight(1)
                    self.devices_layout.addWidget(separator);self._device_rows.append(separator)
                row=QFrame(self.devices_body);row.setObjectName('accountDevice')
                row_layout=QVBoxLayout(row);row_layout.setContentsMargins(8,7,8,7);row_layout.setSpacing(8)
                horizontal=QHBoxLayout();horizontal.setSpacing(9);row_layout.addLayout(horizontal)
                glyph=AccountGlyph(row,device=True,platform=str(device.get('platform','')))
                glyph.dark=self._dark;horizontal.addWidget(glyph)
                detail=QVBoxLayout();detail.setSpacing(3)
                device_name=_label(str(device.get('name') or '未命名设备'),row)
                device_name.setObjectName('accountDeviceName')
                detail.addWidget(device_name)
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
        self._reset_password();self._cancel_rename()
        super().hideEvent(event)

    def apply_theme(self,theme):
        self._dark=theme=='dark'
        card,field,ink,secondary,edge,hover=(('#272c35','#20252e','#edf2fa','#b1bed0','#3c4554','#39465a') if self._dark
            else ('#ffffff','#fbfcfe','#253246','#58697e','#dce3ed','#edf4fe'))
        inset='#20252e' if self._dark else '#f7f9fc'
        if sys.platform=='darwin':
            card='rgba(39,44,53,246)' if self._dark else 'rgba(255,255,255,248)'
            inset='rgba(32,37,46,230)' if self._dark else 'rgba(247,249,252,230)'
        success='#7dd6a8' if self._dark else '#14784a'
        self.setStyleSheet(f'''
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
        ''')
        for button in [*self.device_buttons.values(),*self.device_rename_buttons.values()]:button.dark=self._dark;button.update()
        for glyph in self.findChildren(AccountGlyph):glyph.dark=self._dark;glyph.update()

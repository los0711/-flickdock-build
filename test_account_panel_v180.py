"""Actual account form + AccountService, with isolated in-memory API/session IO."""
import os
import sys
os.environ['QT_QPA_PLATFORM']='offscreen'
if len(sys.argv)>1 and sys.argv[1] in ('1.0','1.25','2.0'):
    os.environ['QT_SCALE_FACTOR']=sys.argv.pop(1)

import copy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QWidget

import app_settings
import account_service as service_module
from account_panel import AccountPanel
from account_service import AccountService
from cloud_api import CloudError

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
QFontDatabase.addApplicationFont(str(Path(__file__).with_name('fonts')/'HarmonyOS_Sans_SC_Regular.ttf'))
APP.setFont(QFont('HarmonyOS Sans SC',11))
PASSWORD='private-fixture-password'
CURRENT='11111111-1111-4111-8111-111111111111'
OTHER='22222222-2222-4222-8222-222222222222'
CHALLENGE='33333333-3333-4333-8333-333333333333'


def flush():
    APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def wait(predicate,timeout=4):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:flush();time.sleep(.002)
    return bool(predicate())


def challenge():
    return dict(schema_version=1,challenge_id=CHALLENGE,salt='01'*16,
        kdf=dict(name='scrypt',N=16384,r=8,p=5,dkLen=32),
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace('+00:00','Z'))


class MemorySessions:
    def __init__(self):self.value=None
    def load(self):return None
    def save(self,value):self.value=copy.deepcopy(value)
    def clear(self):self.value=None


class Api(QObject):
    unauthorized=Signal()
    def __init__(self):super().__init__();self.token='';self.requests=[]
    def request(self,method,path,callback,**options):
        self.requests.append(dict(method=method,path=path,callback=callback,options=copy.deepcopy(options),done=False))
        return str(len(self.requests))
    def last(self,path=None):
        return next(r for r in reversed(self.requests) if path is None or r['path']==path)
    def finish(self,request,result=None,error=None):
        request['done']=True;request['callback'](result,error);flush()
    def cancel(self,key):
        request=self.requests[int(key)-1]
        self.finish(request,error=CloudError('cancelled',code='cancelled'))
    def cancel_all(self):pass
    def close(self):pass


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='flickdock-account-panel-')
        self.base=Path(self.temporary.name)
        self.env=patch.dict(os.environ,APPDATA=str(self.base));self.env.start()
        self.registry_read=patch.object(app_settings,'_read_startup_value');self.read=self.registry_read.start()
        self.registry_write=patch.object(app_settings,'_write_startup_value');self.write=self.registry_write.start()
        self.runtime=patch.object(app_settings,'_startup_runtime_allowed',return_value=False);self.runtime.start()
        self.settings=app_settings.SettingsStore(self.base/'profile');self.settings.update(dock_length=90)
        self.api=Api();self.sessions=MemorySessions()
        self.service=AccountService(self.base,api=self.api,store=self.sessions)
        self.service.device_name='Offscreen fixture'
        self.dialog=app_settings.SettingsDialog(self.settings)
        self.panel=AccountPanel(self.service)
        self.dialog.set_account_panel(self.panel);self.dialog.show();self.dialog.resize(892,632);flush()
        self.click(self.panel.unsigned_profile_button)

    def tearDown(self):
        self.service.close();self.assertTrue(wait(lambda:not self.service._auth_jobs))
        self.dialog.close();self.dialog.deleteLater();flush()
        self.read.assert_not_called();self.write.assert_not_called()
        self.runtime.stop();self.registry_write.stop();self.registry_read.stop();self.env.stop()
        self.temporary.cleanup()

    def click(self,button):
        QTest.mouseClick(button,Qt.MouseButton.LeftButton);flush()

    def account(self,name='甲账号',key='account-a',email=None):
        return dict(id=key,username=name,email=email,email_verified_at='2026-10-26T00:00:00Z' if email else None)

    def sign_in(self,account=None,*,devices=True):
        account=account or self.account()
        if not self.panel.auth_dialog.isVisible():self.click(self.panel.unsigned_profile_button)
        self.panel.username_edit.setText(account['username']);self.panel.password_edit.setText(PASSWORD)
        self.click(self.panel.submit_button)
        self.finish_challenge('login')
        request=self.api.last('/v1/auth/login')
        self.api.finish(request,dict(account=account,token='in-memory-fixture-token',device_id=CURRENT))
        if devices:self.api.finish(self.api.last('/v1/devices'),dict(devices=self.devices()))
        return account

    def finish_challenge(self,purpose='login'):
        request=self.api.last('/v1/auth/challenge')
        self.assertEqual(request['options']['data']['purpose'],purpose)
        self.assertNotIn('password',request['options']['data'])
        count=len(self.api.requests);self.api.finish(request,challenge())
        self.assertTrue(wait(lambda:len(self.api.requests)>count))
        request=self.api.last('/v1/auth/'+purpose)
        self.assertEqual(set(request['options']['data']),{'username','challenge_id','auth_key','device_name','platform'})
        self.assertEqual(len(request['options']['data']['auth_key']),64)
        self.assertNotIn(PASSWORD,str(request['options']['data']))
        return request

    def devices(self):
        return [dict(id=CURRENT,name='当前电脑',platform='windows',current=False,revoked_at=None,last_seen_at='2026-10-26T00:00:00Z'),
                dict(id=OTHER,name='另一台 Mac',platform='macos',current=False,revoked_at=None,last_seen_at='2026-10-26T00:00:00Z'),
                dict(id='revoked',name='已移除的电脑',platform='windows',current=False,revoked_at='2026-10-25T00:00:00Z')]

    def test_register_only_username_password_and_enter_never_saves_settings(self):
        original=self.settings.path.read_bytes()
        self.click(self.panel.register_tab)
        self.assertEqual(set(self.panel.auth_page.findChildren(QLineEdit)),{self.panel.username_edit,self.panel.password_edit})
        self.assertFalse(any('邮箱' in w.accessibleName() for w in self.panel.findChildren(QLineEdit)))
        self.panel.username_edit.setText('新账户');self.panel.password_edit.setText(PASSWORD)
        QTest.keyClick(self.panel.password_edit,Qt.Key.Key_Return);flush()
        request=self.finish_challenge('register')
        self.assertEqual(set(request['options']['data']),{'username','challenge_id','auth_key','device_name','platform'})
        self.assertFalse(request['options']['authenticated'])
        self.assertFalse(self.panel.submit_button.isEnabled());self.assertFalse(self.panel.register_tab.isEnabled())
        self.assertTrue(self.dialog.isVisible());self.assertEqual(self.settings.path.read_bytes(),original)
        self.api.finish(request,dict(account=self.account(),token='fixture-token',device_id=CURRENT))
        self.assertEqual(self.panel.password_edit.text(),'')
        self.assertTrue(self.panel.signed_in_page.isVisible());self.assertFalse(self.panel.auth_page.isVisible())
        self.assertNotIn(PASSWORD,str(self.sessions.value))
        self.assertEqual(self.settings.path.read_bytes(),original)

    def test_auth_errors_inline_redact_password_and_switch_clears_password(self):
        before=len(self.api.requests);self.click(self.panel.submit_button)
        self.assertEqual(len(self.api.requests),before);self.assertIn('账号',self.panel.auth_status_label.text())
        self.assertTrue(self.panel.auth_status_label.isVisible())
        self.assertIn('账号',self.panel.auth_status_label.text())
        self.assertGreater(self.panel.auth_status_label.y(),self.panel.submit_button.y())
        self.panel.username_edit.setText('甲账号');self.panel.password_edit.setText(PASSWORD)
        self.click(self.panel.submit_button)
        self.finish_challenge()
        self.api.finish(self.api.last(),error=CloudError('rejected '+PASSWORD,401,'INVALID_CREDENTIALS'))
        self.assertNotIn(PASSWORD,self.panel.auth_status_label.text())
        self.assertIn('密码不正确',self.panel.auth_status_label.text());self.assertEqual(self.panel.password_edit.text(),'')
        self.assertTrue(self.panel.submit_button.isEnabled());self.assertTrue(self.panel.auth_page.isVisible())
        self.click(self.panel.register_tab);self.assertEqual(self.panel.password_edit.text(),'')
        self.panel.password_edit.setText(PASSWORD);self.dialog.hide();flush()
        self.assertEqual(self.panel.password_edit.text(),'')

    def test_existing_email_metadata_stays_private_and_sync_status_works(self):
        account=self.sign_in(self.account(name='<b>用户名</b>',email='saved@example.invalid'))
        self.assertEqual(self.panel.username_label.text(),account['username'])
        self.assertEqual(self.panel.username_label.textFormat(),Qt.TextFormat.PlainText)
        self.assertEqual(self.service.account['email'],'saved@example.invalid')
        visible=' '.join(label.text() for label in self.panel.findChildren(QLabel))
        visible+=' '+' '.join(button.text() for button in self.panel.findChildren(QPushButton))
        self.assertNotIn('邮箱',visible);self.assertNotIn('验证码',visible)
        self.assertNotIn('saved@example.invalid',visible)
        self.assertFalse(any('邮箱' in edit.accessibleName() for edit in self.panel.findChildren(QLineEdit)))
        before=len(self.api.requests)
        self.panel.set_sync_status('已同步');self.assertEqual(self.panel.sync_status_label.text(),'已同步')
        self.assertEqual(len(self.api.requests),before)

    def test_device_current_tag_revoked_filter_inline_confirm_cancel_and_retry(self):
        self.sign_in()
        self.assertEqual(set(self.panel.device_buttons),{OTHER})
        visible=' '.join(label.text() for label in self.panel.devices_body.findChildren(QLabel))
        self.assertIn('(本机)',visible);self.assertNotIn('已移除的电脑',visible)
        top={id(w) for w in APP.topLevelWidgets()}
        before=len(self.api.requests);self.click(self.panel.device_buttons[OTHER])
        self.assertTrue(self.panel.confirm_frame.isVisible())
        confirmation_row=self.panel.devices_layout.getItemPosition(self.panel.devices_layout.indexOf(self.panel.confirm_frame))[0]
        device_row=self.panel.devices_layout.getItemPosition(self.panel.devices_layout.indexOf(self.panel._rows_by_id[OTHER]))[0]
        self.assertGreater(confirmation_row,device_row)
        self.assertEqual(top,{id(w) for w in APP.topLevelWidgets()})
        self.click(self.panel.cancel_remove_button);self.assertEqual(len(self.api.requests),before)
        self.click(self.panel.device_buttons[OTHER]);self.click(self.panel.confirm_remove_button)
        self.assertEqual(self.api.last()['method'],'DELETE');self.assertTrue(self.api.last()['path'].endswith(OTHER))
        self.api.finish(self.api.last(),error=RuntimeError('offline'))
        self.assertTrue(self.panel.device_buttons[OTHER].isEnabled());self.assertFalse(self.panel.confirm_frame.isVisible())
        self.click(self.panel.device_buttons[OTHER]);self.click(self.panel.confirm_remove_button)
        self.api.finish(self.api.last(),dict(revoked=True))
        self.api.finish(self.api.last('/v1/devices'),dict(devices=self.devices()[:1]))
        self.assertEqual(self.panel.device_buttons,{})
        before=len(self.api.requests);self.panel._ask_remove(CURRENT);self.panel._confirm_remove()
        self.assertEqual(len(self.api.requests),before)

    def test_logout_and_account_switch_clear_identity_devices_and_stale_results(self):
        self.sign_in(self.account(email='account-a@example.invalid'))
        self.panel.set_sync_status('账号甲文件正在同步')
        self.panel.refresh_devices();old_devices=self.api.last('/v1/devices')
        self.click(self.panel.logout_button);self.api.finish(self.api.last('/v1/auth/logout'),dict())
        self.assertTrue(self.panel.unsigned_profile_button.isVisible());self.assertFalse(self.panel.auth_dialog.isVisible());self.assertFalse(self.panel.signed_in_page.isVisible())
        self.assertEqual(self.panel.username_label.text(),'')
        self.assertEqual(self.panel.sync_status_label.text(),'');self.assertEqual(self.panel.device_buttons,{})
        self.panel.set_sync_status('stale A sync');self.assertEqual(self.panel.sync_status_label.text(),'')
        self.sign_in(self.account(name='乙账户',key='account-b'))
        self.api.finish(old_devices,dict(devices=[dict(id=OTHER,name='账号甲专用设备',platform='macos')]))
        self.service.email_challenge.emit('stale-a-challenge','new-a@example.invalid');flush()
        text=' '.join(label.text() for label in self.panel.findChildren(QLabel))
        self.assertNotIn('account-a@example.invalid',text);self.assertNotIn('账号甲',text)
        self.assertNotIn('stale A',text);self.assertEqual(self.panel.username_label.text(),'乙账户')
        self.assertNotIn('account-a',str(self.sessions.value))

    def test_legacy_email_binding_option_does_not_recreate_ui(self):
        other=AccountPanel(self.service,email_binding=True)
        labels=' '.join(label.text() for label in other.findChildren(QLabel))
        self.assertNotIn('邮箱',labels)
        self.assertNotIn('验证码',labels)
        self.assertFalse(any('邮箱' in edit.accessibleName() for edit in other.findChildren(QLineEdit)))
        other.deleteLater();flush()

    def test_light_dark_reference_settings_surface_preserves_account_controls(self):
        for signed in (False,True):
            if signed:
                long_name='用户名'*20
                self.sign_in(self.account(name=long_name,email='long-mailbox@example.invalid'))
                self.assertEqual(self.panel.username_label.text(),long_name)
                self.assertEqual(self.panel.username_label.accessibleName(),long_name)
                self.assertTrue(QLabel.text(self.panel.username_label).endswith('…'))
                self.assertGreaterEqual(self.panel.username_label.height(),self.panel.username_label.fontMetrics().height())
            for theme in ('light','dark'):
                self.dialog.apply_theme(theme);flush()
                self.assertEqual(self.dialog.width(),892)
                self.assertEqual(self.dialog.surface.width(),860)
                self.assertEqual(self.dialog.account_slot_layout.itemAt(1).widget(),self.panel)
                self.assertLess(self.panel.username_label.geometry().right(),self.panel.account_state_label.geometry().left())
                for widget in self.panel.findChildren(QWidget):
                    if isinstance(widget,(QLineEdit,QPushButton)) and widget.isVisible():
                        container=self.panel.auth_dialog if widget.window() is self.panel.auth_dialog else self.panel
                        point=widget.mapTo(container,QPoint())
                        self.assertGreaterEqual(point.x(),0)
                        self.assertLessEqual(point.x()+widget.width(),container.width())
                self.assertEqual(self.dialog.scroll.horizontalScrollBar().maximum(),0)
                self.assertTrue(self.dialog.surface.rect().contains(self.dialog.save_button.geometry()))
                self.assertTrue(self.dialog.surface.rect().contains(self.dialog.cancel_button.geometry()))
                target=os.getenv('FLICKDOCK_ACCOUNT_RENDER_DIR')
                if target:
                    folder=Path(target);folder.mkdir(parents=True,exist_ok=True)
                    self.dialog.grab().save(str(folder/f'account-{theme}-{signed}-{os.getenv("QT_SCALE_FACTOR","1")}.png'))
                    self.panel.grab().save(str(folder/f'panel-{theme}-{signed}-{os.getenv("QT_SCALE_FACTOR","1")}.png'))


if __name__=='__main__':unittest.main(verbosity=2)

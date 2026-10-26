"""Account/device/email actions for the shared Windows/macOS cloud protocol."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import platform
import re
import sys
import threading
from urllib.parse import quote

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import QApplication

from account_store import SessionStore
from cloud_api import CloudApi
from cloud_config import DEFAULT_CLOUD_URL


AUTH_KDF = {'name':'scrypt','N':16384,'r':8,'p':5,'dkLen':32}


def _wipe(value):
    if isinstance(value,bytearray):value[:]=b'\0'*len(value)
    elif isinstance(value,dict):
        for child in value.values():_wipe(child)


def validate_auth_challenge(value):
    """A server cannot choose a more expensive KDF or a different protocol."""
    if (not isinstance(value,dict) or type(value.get('schema_version')) is not int
            or value.get('schema_version')!=1):
        raise ValueError('账号验证参数无效，请稍后重试。')
    challenge,salt,parameters=value.get('challenge_id'),value.get('salt'),value.get('kdf')
    if (not isinstance(challenge,str) or not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}',challenge)
            or not isinstance(salt,str) or not re.fullmatch(r'[0-9a-f]{32}',salt)
            or not isinstance(parameters,dict) or parameters!=AUTH_KDF
            or any(type(parameters.get(k)) is not int for k in ('N','r','p','dkLen'))):
        raise ValueError('账号验证参数无效，请稍后重试。')
    expires=value.get('expires_at')
    try:
        if not isinstance(expires,str) or len(expires)>40:raise ValueError()
        expires=datetime.fromisoformat(expires.replace('Z','+00:00'))
        if expires.tzinfo is None or expires.utcoffset()!=timezone.utc.utcoffset(expires):raise ValueError()
        if expires<=datetime.now(timezone.utc):raise ValueError()
    except (ValueError,OverflowError):
        raise ValueError('登录验证已过期或格式无效，请重试。') from None
    return challenge,bytes.fromhex(salt),expires


def derive_auth_key(password,salt):
    """Called only on a worker; parameters never come from untrusted JSON."""
    return bytearray(hashlib.scrypt(password,salt=salt,n=16384,r=8,p=5,dklen=32,maxmem=32*1024*1024))


class _AuthSignals(QObject):
    finished=Signal(int,object,str)


class _AuthWorker(QRunnable):
    def __init__(self,epoch,password,salt):
        super().__init__()
        self.epoch,self.password,self.salt=epoch,password,salt
        self.cancelled=threading.Event();self.signals=_AuthSignals();self.proof=None

    def cancel(self):
        self.cancelled.set();_wipe(self.proof)

    def run(self):
        error=''
        try:
            if not self.cancelled.is_set():self.proof=derive_auth_key(self.password,self.salt)
            if self.cancelled.is_set():_wipe(self.proof);self.proof=None
        except Exception:
            _wipe(self.proof);self.proof=None
            error='本机无法完成账号验证，请稍后重试。'
        finally:
            _wipe(self.password);self.password=None
        self.signals.finished.emit(self.epoch,self.proof,error)


class _PasswordWorker(_AuthWorker):
    """Both password derivations stay off the GUI thread and share cancellation."""
    def __init__(self,epoch,password,salt,new_password,new_salt):
        super().__init__(epoch,password,salt)
        self.new_password,self.new_salt=new_password,new_salt

    def run(self):
        error='';self.proof={}
        try:
            if not self.cancelled.is_set():self.proof['old']=derive_auth_key(self.password,self.salt)
            if not self.cancelled.is_set():self.proof['new']=derive_auth_key(self.new_password,self.new_salt)
            if self.cancelled.is_set():_wipe(self.proof);self.proof=None
        except Exception:
            _wipe(self.proof);self.proof=None
            error='本机无法完成密码验证，请稍后重试。'
        finally:
            _wipe(self.password);_wipe(self.new_password)
            self.password=self.new_password=None
        self.signals.finished.emit(self.epoch,self.proof,error)


class AccountService(QObject):
    account_changed = Signal(object)
    status_changed = Signal(str)
    busy_changed = Signal(bool)
    devices_changed = Signal(list)
    email_challenge = Signal(str, str)
    password_changed = Signal()

    def __init__(self, base, parent=None, *, origin=DEFAULT_CLOUD_URL, api=None, store=None):
        super().__init__(parent)
        application = QApplication.instance()
        native = application is not None and application.platformName() not in ('offscreen','minimal')
        self.api = api or CloudApi(origin, self)
        self.store = store or SessionStore(base, origin, enabled=native)
        self.account, self.device_id = None, ''
        self.busy, self.epoch = False, 0
        self._closed=False
        self._auth_state=None;self._auth_request_key=None;self._auth_jobs={}
        self.platform = {'win32':'windows','darwin':'macos'}.get(sys.platform,'linux')
        self.device_name = platform.node()[:80] or ('Mac' if self.platform=='macos' else 'Windows PC')
        self._restored = self.store.load() if native or store is not None else None
        self.api.unauthorized.connect(self._expired)

    @property
    def signed_in(self): return bool(self.account and self.api.token)

    def _busy(self, value):
        self.busy = bool(value); self.busy_changed.emit(self.busy)

    def resume(self):
        session, self._restored = self._restored, None
        if not session:
            self.account_changed.emit(None); return
        self.api.token = session['token']; self.device_id = session.get('device_id','')
        # Resume the account's own local cache even while offline; only this
        # account's session can later upload it. A server401 revokes it.
        self.account = session['account']; self.account_changed.emit(dict(self.account))
        self.refresh()

    def register(self, username, password): return self._auth('register', username, password)
    def login(self, username, password): return self._auth('login', username, password)

    def change_password(self, old_password, new_password, confirm_password=None):
        if self._closed or not self.signed_in or self.busy:return False
        if not isinstance(old_password,str) or not old_password:
            self.status_changed.emit('请输入原密码。');return False
        if not isinstance(new_password,str) or not 10<=len(new_password)<=128 or len(old_password)>128:
            self.status_changed.emit('新密码需要 10–128 个字符。');return False
        if confirm_password is not None and new_password!=confirm_password:
            self.status_changed.emit('两次输入的新密码不一致。');return False
        if old_password==new_password:
            self.status_changed.emit('新密码不能与原密码相同。');return False
        old_secret=None
        try:old_secret,new_secret=bytearray(old_password.encode('utf8')),bytearray(new_password.encode('utf8'))
        except UnicodeError:
            _wipe(old_secret)
            self.status_changed.emit('密码格式无效，请重新输入。');return False
        self.epoch+=1;epoch=self.epoch
        self._auth_state=dict(epoch=epoch,action='password',password=old_secret,new_password=new_secret)
        self._busy(True);self.status_changed.emit('正在修改密码…')
        try:
            self._auth_request_key=self.api.request('POST','/v1/auth/password/challenge',
                lambda result,error:self._challenge_done(epoch,result,error),data={})
        except Exception:self._auth_error(epoch,'账号服务暂时无法连接，请稍后重试。')
        return True

    def _auth(self, action, username, password):
        if self._closed or self.busy: return False
        if not isinstance(username,str) or not username.strip() or not isinstance(password,str) or not password:
            self.status_changed.emit('请输入账号和密码。'); return False
        if not 10<=len(password)<=128:
            self._busy(False);self.status_changed.emit('密码需要 10–128 个字符。');return False
        try:secret=bytearray(password.encode('utf8'))
        except UnicodeError:
            self._busy(False);self.status_changed.emit('密码格式无效，请重新输入。');return False
        self.epoch += 1; epoch = self.epoch
        self._auth_state=dict(epoch=epoch,action=action,username=username.strip(),password=secret)
        self._busy(True); self.status_changed.emit('正在注册…' if action=='register' else '正在登录…')
        if not self._auth_live(epoch):return False
        try:
            self._auth_request_key=self.api.request('POST','/v1/auth/challenge',
                lambda result,error:self._challenge_done(epoch,result,error),authenticated=False,
                data={'username':username.strip(),'purpose':action})
        except Exception:self._auth_error(epoch,'账号服务暂时无法连接，请稍后重试。')
        return True

    def _auth_live(self,epoch):
        return not self._closed and epoch==self.epoch and self._auth_state is not None and self._auth_state['epoch']==epoch

    def _auth_error(self,epoch,message):
        if not self._auth_live(epoch):return
        self._drop_auth_state();self._busy(False)
        if not self._closed and epoch==self.epoch:self.status_changed.emit(message)

    @staticmethod
    def _auth_message(error):
        # Server error text must never echo a password, proof, or provider data.
        return {'INVALID_CREDENTIALS':'账号或密码不正确。','USERNAME_TAKEN':'这个账号已被使用。',
                'INVALID_CHALLENGE':'登录验证已过期，请重试。','AUTH_NOT_CONFIGURED':'账号服务暂未开放，请稍后重试。',
                'INVALID_OLD_PASSWORD':'原密码不正确，请重新输入。',
                'PASSWORD_CHANGED':'密码已在其他设备更改，请重新验证原密码。',
                'DEVICE_LIMIT':'账号已登录 20 台设备，请先移除其他设备后再登录。',
                'EMAIL_UNAVAILABLE':'邮箱验证服务暂未开放，请稍后重试。',
                'INVALID_EMAIL':'邮箱地址格式无效，请重新输入。',
                'INVALID_CODE':'验证码无效或已过期，请重新获取。',
                'RATE_LIMITED':'操作过于频繁，请稍后重试。','INVALID_USERNAME':'账号格式无效，请重新输入。',
                'cancelled':'账号操作已取消。'}.get(getattr(error,'code',''),'账号操作失败，请检查网络后重试。')

    def _challenge_done(self,epoch,result,error):
        if not self._auth_live(epoch):return
        self._auth_request_key=None
        if error:self._auth_error(epoch,self._auth_message(error));return
        try:challenge,salt,expires=validate_auth_challenge(result)
        except ValueError as problem:self._auth_error(epoch,str(problem));return
        state=self._auth_state;state.update(challenge_id=challenge,expires=expires)
        if state['action']=='password':
            new_salt=result.get('new_salt')
            if not isinstance(new_salt,str) or not re.fullmatch(r'[0-9a-f]{32}',new_salt) or new_salt==result['salt']:
                self._auth_error(epoch,'密码验证参数无效，请稍后重试。');return
            job=_PasswordWorker(epoch,state.pop('password'),salt,state.pop('new_password'),bytes.fromhex(new_salt))
        else:job=_AuthWorker(epoch,state.pop('password'),salt)
        self._auth_jobs[epoch]=job
        job.signals.finished.connect(self._auth_derived,Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(job)

    def _auth_derived(self,epoch,proof,error):
        job=self._auth_jobs.pop(epoch,None)
        try:
            if not self._auth_live(epoch):return
            state=self._auth_state
            if state['action']=='password':
                if error or not isinstance(proof,dict) or any(not isinstance(proof.get(k),bytearray) or len(proof[k])!=32 for k in ('old','new')):
                    self._auth_error(epoch,error or '本机未能完成密码验证，请重试。');return
                if state['expires']<=datetime.now(timezone.utc):
                    self._auth_error(epoch,'密码验证已过期，请重试。');return
                self._auth_request_key=self.api.request('POST','/v1/auth/password',
                    lambda result,problem:self._auth_done(epoch,result,problem),
                    data={'challenge_id':state['challenge_id'],'old_auth_key':proof['old'].hex(),'new_auth_key':proof['new'].hex()})
                return
            if error or not isinstance(proof,bytearray) or len(proof)!=32:
                self._auth_error(epoch,error or '本机未能完成账号验证，请重试。');return
            state=self._auth_state
            if state['expires']<=datetime.now(timezone.utc):
                self._auth_error(epoch,'登录验证已过期，请重试。');return
            self._auth_request_key=self.api.request('POST','/v1/auth/'+state['action'],
                lambda result,problem:self._auth_done(epoch,result,problem),authenticated=False,
                data={'username':state['username'],'challenge_id':state['challenge_id'],'auth_key':proof.hex(),
                      'device_name':self.device_name,'platform':self.platform})
        except Exception:self._auth_error(epoch,'账号服务暂时无法连接，请稍后重试。')
        finally:
            _wipe(proof)
            if job is not None:job.proof=None

    def _auth_done(self,epoch,result,error):
        if not self._auth_live(epoch):return
        self._auth_request_key=None
        if error:self._auth_error(epoch,self._auth_message(error));return
        if self._auth_state['action']=='password':
            if not isinstance(result,dict) or result.get('changed') is not True:
                self._auth_error(epoch,'账号服务未能确认密码已修改，请重试。');return
            self._drop_auth_state();self._busy(False)
            self.password_changed.emit();self.status_changed.emit('密码已修改，其他设备需要重新登录。');self.list_devices()
            return
        account=result.get('account') if isinstance(result,dict) else None
        if (not isinstance(account,dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(account.get('id','')))
                or not isinstance(result.get('token'),str) or not result['token']):
            self._auth_error(epoch,'账号服务返回了无效登录状态。');return
        account={key:account.get(key) for key in ('id','username','email','email_verified_at')}
        session={key:result[key] for key in ('token','device_id','expires_at') if key in result}
        session['account']=account
        self.api.token=result['token'];self.account=account;self.device_id=result.get('device_id','')
        self._drop_auth_state();self._busy(False)
        if self._closed or epoch!=self.epoch:return
        notice='登录成功，正在同步搁架。'
        try:self.store.save(session)
        except Exception:notice='已登录，本机无法安全保存凭据，重启后需重新登录。'
        self.account_changed.emit(dict(account));self.status_changed.emit(notice);self.list_devices()

    def _drop_auth_state(self):
        state,self._auth_state=self._auth_state,None
        self._auth_request_key=None
        if state is not None:_wipe(state.pop('password',None));_wipe(state.pop('new_password',None));state.clear()

    def cancel_auth(self):
        active=self._auth_state is not None
        key=self._auth_request_key
        self.epoch+=1;self._drop_auth_state()
        for job in self._auth_jobs.values():job.cancel()
        if key is not None:
            try:self.api.cancel(key)
            except (AttributeError,RuntimeError):pass
        if active:
            self._busy(False)
            if not self._closed:self.status_changed.emit('账号操作已取消。')
        return active

    def refresh(self):
        if not self.api.token: return
        epoch = self.epoch
        def done(result,error):
            if epoch != self.epoch: return
            if error: self.status_changed.emit(self._auth_message(error)); return
            account = result.get('account',result.get('user'))
            if isinstance(account,dict):
                self.account = account; self.account_changed.emit(dict(account))
            self.list_devices()
        self.api.request('GET','/v1/me',done)

    def list_devices(self):
        if not self.signed_in: return
        epoch = self.epoch
        def done(result,error):
            if epoch != self.epoch: return
            if error: self.status_changed.emit(self._auth_message(error)); return
            self.devices_changed.emit(result.get('devices',[]))
        self.api.request('GET','/v1/devices',done)

    def revoke_device(self, device_id):
        if not self.signed_in: return
        epoch = self.epoch
        def done(result,error):
            if epoch != self.epoch: return
            if error: self.status_changed.emit(self._auth_message(error)); return
            if device_id == self.device_id: self._clear('此设备已退出登录。')
            else: self.status_changed.emit('已移除该设备的登录。'); self.list_devices()
        self.api.request('DELETE','/v1/devices/'+quote(device_id,safe=''),done)

    def rename_device(self, device_id, name):
        if not self.signed_in or self.busy:return False
        if not isinstance(name,str):self.status_changed.emit('请输入设备名称。');return False
        name=name.strip()
        try:valid=bool(name) and len(name.encode('utf-16-le'))<=160 and not re.search(r'[\x00-\x1f\x7f]',name)
        except UnicodeError:valid=False
        if not valid:self.status_changed.emit('设备名称需要 1–80 个字符，且不能包含控制字符。');return False
        self._busy(True);epoch=self.epoch
        def done(result,error):
            if self._closed or epoch!=self.epoch:return
            self._busy(False)
            if error:self.status_changed.emit(self._auth_message(error));return
            if device_id==self.device_id:self.device_name=name
            self.status_changed.emit('设备名称已更新。');self.list_devices()
        try:self.api.request('PATCH','/v1/devices/'+quote(device_id,safe=''),done,data={'name':name})
        except Exception:
            self._busy(False);self.status_changed.emit('账号服务暂时无法连接，请稍后重试。');return False
        return True

    def request_email_code(self, email):
        if not self.signed_in or self.busy: return
        self._busy(True); epoch = self.epoch
        def done(result,error):
            if epoch != self.epoch: return
            self._busy(False)
            if error: self.status_changed.emit(self._auth_message(error)); return
            self.email_challenge.emit(result.get('challenge_id',''),email)
            self.status_changed.emit('验证码已发送，请查收邮箱。')
        self.api.request('POST','/v1/email/code',done,data={'email':email})

    def bind_email(self, challenge_id, code):
        if not self.signed_in or self.busy: return
        self._busy(True); epoch = self.epoch
        def done(result,error):
            if epoch != self.epoch: return
            self._busy(False)
            if error: self.status_changed.emit(self._auth_message(error)); return
            self.status_changed.emit('邮箱已绑定。'); self.refresh()
        self.api.request('POST','/v1/email/bind',done,data={'challenge_id':challenge_id,'code':code})

    def logout(self):
        self.cancel_auth()
        if not self.api.token: self._clear('已退出登录。'); return
        self._busy(True); epoch = self.epoch
        def done(result,error):
            if epoch != self.epoch: return
            if error:
                self._busy(False); self.status_changed.emit('暂时无法退出云端设备，请联网后重试。'); return
            self._clear('已退出登录。')
        self.api.request('POST','/v1/auth/logout',done,data={})

    def _expired(self): self._clear('登录已过期或该设备已被移除，请重新登录。')

    def _clear(self, message):
        self.cancel_auth()
        self.api.token = ''; self.account = None; self.device_id = ''
        self.api.cancel_all()
        try:self.store.clear()
        except OSError:pass
        self._busy(False)
        self.account_changed.emit(None); self.devices_changed.emit([]); self.status_changed.emit(message)

    def close(self):
        if self._closed:return
        self._closed=True;self.cancel_auth();self.api.close()

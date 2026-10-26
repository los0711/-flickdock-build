"""Explicit frozen QA entry, offscreen profiles and no OS credential writes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from uuid import uuid4


def run(argv,load_font):
    if os.environ.get('QT_QPA_PLATFORM') != 'offscreen': raise RuntimeError('Cloud verification requires offscreen')
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from account_service import AccountService,derive_auth_key
    from cloud_config import DEFAULT_CLOUD_URL
    from shelf_sync import ShelfSync
    from app_version import APP_VERSION
    app=QApplication([argv[0]]);app.setQuitOnLastWindowClosed(False);load_font(app)
    output=Path(argv[argv.index('--verify-output')+1]).resolve()
    report={'ok':False,'version':APP_VERSION,'frozen':bool(getattr(sys,'frozen',False)),
            'os_credentials_written':False,'installer_executed':False,'mac_application_tested':False}
    # This catches accidental _hashlib trimming in the distributed executable.
    key=derive_auth_key(bytearray(b'frozen-qa-known-password'),b'\x01'*16)
    assert bytes(key).hex() == '61a958177a02aa9f93c69ce6fbf35b66442e1bf16c556bd6405b89b86dd4a6fa'
    report['scrypt_fixture']=hashlib.sha256(key).hexdigest()
    if sys.platform=='win32':
        from keyring.backends.Windows import WinVaultKeyring
        assert WinVaultKeyring.priority > 0
    elif sys.platform=='darwin':
        from keyring.backends.macOS import Keyring
        assert Keyring.priority > 0
    class Store:
        def load(self): return None
        def save(self,value): pass
        def clear(self): pass
    class Adapter:
        def __init__(self): self.view={'permanent':[],'texts':{},'metadata':{}}
        def snapshot(self): return copy.deepcopy(self.view)
        def restore(self,value): self.view=copy.deepcopy(value)
        def title(self,ref): return '文字片段' if ref.startswith('flickdock:') else Path(ref).name
    with tempfile.TemporaryDirectory(prefix='flickdock-frozen-cloud-') as directory:
        root=Path(directory);payload=bytes(range(256))*10000;source=root/'同步验证.bin';source.write_bytes(payload)
        username='frozen-qa-'+uuid4().hex;password=secrets.token_urlsafe(32)
        adapters=[Adapter(),Adapter()];adapters[0].view={'permanent':[str(source),'flickdock:text:qa'],
            'texts':{'flickdock:text:qa':'同步文字仍可直接拖入输入框'},'metadata':{}}
        services=[AccountService(root/str(i),origin=DEFAULT_CLOUD_URL,store=Store()) for i in range(2)]
        engines=[ShelfSync(root/str(i),services[i],adapters[i],poll_ms=600000) for i in range(2)]
        finished=False;phase='register'
        def finish(error=''):
            nonlocal finished
            if finished:return
            finished=True
            report.update(ok=not error,error=error)
            for engine in engines:engine.close()
            for service in services:service.close()
            output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            app.exit(1 if error else 0)
        def first_account(account):
            nonlocal phase
            if account and phase=='register':
                phase='upload';report['probe_account_id']=account['id']
        def first_sync(status):
            nonlocal phase
            if phase=='upload' and status=='搁架已同步。':
                phase='login';services[1].platform='macos';services[1].device_name='Frozen macOS protocol verification'
                services[1].login(username,password)
        def second_sync(status):
            nonlocal phase
            if phase=='login' and status=='搁架已同步。':
                try:
                    refs=adapters[1].view['permanent'];assert len(refs)==2
                    target=next(Path(ref) for ref in refs if not ref.startswith('flickdock:'))
                    assert target.read_bytes()==payload
                    assert list(adapters[1].view['texts'].values())==['同步文字仍可直接拖入输入框']
                    report.update(verified_bytes=len(payload),verified_sha256=hashlib.sha256(payload).hexdigest(),text_native=True)
                    phase='logout';services[1].logout();services[0].logout()
                except Exception:finish('冻结版内容校验失败。')
        def logged_out(account):
            if phase=='logout' and not any(s.signed_in for s in services):finish()
        services[0].account_changed.connect(first_account)
        for service in services:service.account_changed.connect(logged_out)
        engines[0].status_changed.connect(first_sync);engines[1].status_changed.connect(second_sync)
        def problem(status):
            if any(word in status for word in ('失败','无法','频繁','暂未','不正确','已过期')):finish(status)
        for service in services:service.status_changed.connect(problem)
        for engine in engines:engine.status_changed.connect(problem)
        QTimer.singleShot(0,lambda:services[0].register(username,password))
        QTimer.singleShot(150000,lambda:finish('云端联调超时。'))
        return app.exec()

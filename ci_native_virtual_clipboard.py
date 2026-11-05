"""Real OLE virtual files on a disposable Windows runner; no user clipboard."""
import ctypes,hashlib,json,os,struct,sys,tempfile,time
from pathlib import Path

def main():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('FD_QA_NATIVE_VIRTUAL')!='1' or os.environ.get('QT_QPA_PLATFORM')!='windows'):
        raise RuntimeError('Native clipboard QA is restricted to the disposable Windows runner')
    from PySide6.QtCore import QMimeData,QUrl
    from PySide6.QtWidgets import QApplication
    from account_service import AccountService
    from cloud_events import CloudEvents
    from clipboard_sync import ClipboardSync
    from virtual_clipboard import DESCRIPTOR_MIME,CONTENT_MIME
    app=QApplication([]);app.setQuitOnLastWindowClosed(False)
    class Store:
        def load(self):return None
        def save(self,value):raise AssertionError('This test must not authenticate')
        def clear(self):pass
    temporary=tempfile.TemporaryDirectory(prefix='flickdock-native-virtual-');root=Path(temporary.name)
    account=AccountService(root/'account',store=Store());events=CloudEvents(account,enabled=False)
    clipboard=app.clipboard();clipboard.clear()
    client=ClipboardSync(account,events,root/'profile',enabled=False)
    errors=[]
    def exception(kind,error,trace):
        errors.append(kind.__name__);sys.__excepthook__(kind,error,trace)
    sys.excepthook=exception
    report=dict(ok=False,platform=sys.platform,qt_platform=app.platformName(),native_clipboard=True,
                disposable_runner=True,network_authenticated=False)
    def until(predicate):
        deadline=time.monotonic()+65
        while time.monotonic()<deadline:
            app.processEvents()
            if errors:raise AssertionError('Qt callback failed: '+','.join(errors))
            if predicate():return
            time.sleep(.002)
        report.update(timeout=True,native_format_issue=client.view.get('native_format_issue'),
                      capture_active=client._virtual_capture is not None,
                      native_retry=client._virtual_retry[0][0] if client._virtual_retry else None)
        for log in (root/'profile').rglob('*diagnostic*'):
            if log.is_file() and log.stat().st_size<65536:print('QA diagnostic:',log.name,log.read_text(encoding='utf8'))
        raise TimeoutError('Native virtual file did not materialize: '+str(client.view.get('native_format_issue')))
    def virtual(name,body):
        raw=bytearray(596);struct.pack_into('<II',raw,0,1,0x40);struct.pack_into('<II',raw,68,0,len(body))
        name_bytes=name.encode('utf-16-le');raw[76:76+len(name_bytes)]=name_bytes
        mime=QMimeData();mime.setData(DESCRIPTOR_MIME,raw);mime.setData(CONTENT_MIME,body);return mime
    try:
        payload=os.urandom(512*1024+13);source=root/'原始文件.zip';source.write_bytes(payload)
        report['phase']='local-file-url'
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);clipboard.setMimeData(mime)
        def source_visible():
            value=client.view.get('value')
            return value is not None and value[0]=='files' and len(value[1])==1 and Path(value[1][0])==source
        until(source_visible);original_value=client.view['value'];report['phase']='equivalent-virtual'
        revision=client.view['revision'];clipboard.setMimeData(virtual(source.name,payload))
        until(lambda:client._virtual_value is not None)
        assert client.view['value']==original_value and client.view['revision']==revision
        assert source.read_bytes()==payload
        report['equivalent_virtual_format_keeps_original_path_and_revision']=True
        first_key=client._virtual_value[0];second=os.urandom(512*1024+7)
        report['phase']='new-virtual'
        clipboard.setMimeData(virtual('另一份文件.zip',second))
        until(lambda:client._virtual_value is not None and client._virtual_value[0]!=first_key)
        value=client.view['value'];assert value[0]=='files' and len(value[1])==1
        received=Path(value[1][0]);assert received.name=='另一份文件.zip' and received.is_relative_to(client.cache)
        assert received.read_bytes()==second and source.read_bytes()==payload
        assert client.view['revision']==revision+1
        report.update(ok=True,verified_bytes=len(payload)+len(second),independent_worker=True,
            new_virtual_file_sha256=hashlib.sha256(second).hexdigest(),new_file_published_once=True,
            native_format_count=int(ctypes.windll.user32.CountClipboardFormats()))
    finally:
        clipboard.clear();client.close();client.pool.waitForDone(10000);events.close();account.close()
        temporary.cleanup();Path('native-virtual-clipboard.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report));return 0

if __name__=='__main__':raise SystemExit(main())

"""Qt production transport against an isolated Worker; no system clipboard."""
import os,sys
NATIVE='--native-ci' in sys.argv
if NATIVE:
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS') not in ('Windows','macOS'):
        raise RuntimeError('Native clipboard QA is restricted to an ephemeral CI runner.')
    os.environ['QT_QPA_PLATFORM']='cocoa' if sys.platform=='darwin' else 'windows'
else:os.environ['QT_QPA_PLATFORM']='offscreen'
import json,tempfile,time
from uuid import uuid4
from pathlib import Path
from PySide6.QtCore import QObject,Signal,QMimeData,QUrl
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication
from cloud_api import CloudApi
from clipboard_sync import ClipboardSync
from cloud_events import CloudEvents

class Clipboard(QObject):
    dataChanged=Signal()
    def __init__(self):super().__init__();self.mime=QMimeData()
    def mimeData(self):return self.mime
    def setMimeData(self,value):self.mime=value;self.dataChanged.emit()
    def clear(self):self.setMimeData(QMimeData())
class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,origin,session):
        super().__init__();self.api=CloudApi(origin,allow_test_http=True);self.api.token=session['token'];self.device_id=session['device_id'];self.signed_in=True;self.account=session.get('account',{})
class Events(QObject):
    clipboard_changed=Signal()
    connected=Signal()
    def __init__(self):super().__init__();self.socket=self

def main():
    # Windowed Windows executables intentionally have no Python stdin object.
    if '--verify-connection' in sys.argv:
        connection=json.loads(Path(sys.argv[sys.argv.index('--verify-connection')+1]).read_text(encoding='utf8'))
    else:connection=json.loads(sys.stdin.read())
    app=QApplication.instance() or QApplication([])
    def until(predicate):
        deadline=time.monotonic()+45
        while not predicate() and time.monotonic()<deadline:app.processEvents();time.sleep(.005)
        assert predicate(),'clipboard operation did not finish'
    with tempfile.TemporaryDirectory(prefix='flickdock-clipboard-http-') as directory:
        clips=[app.clipboard() if NATIVE else Clipboard(),Clipboard()];accounts=[Account(connection['origin'],connection[key]) for key in ('a','b')]
        events=[CloudEvents(account,enabled=False) for account in accounts]
        clients=[ClipboardSync(accounts[i],events[i],Path(directory)/str(i),clipboard=clips[i]) for i in range(2)]
        try:
            # This fixture's authenticated socket is loopback-only. Production
            # CloudEvents._connect continues to require HTTPS/WSS.
            for account,event in zip(accounts,events):
                url=QUrl(connection['origin']);assert url.host() in ('127.0.0.1','localhost')
                url.setScheme('ws');url.setPath('/v1/events')
                request=QNetworkRequest(url);request.setRawHeader(b'Authorization',('Bearer '+account.api.token).encode())
                event.socket.open(request)
            from PySide6.QtNetwork import QAbstractSocket
            until(lambda:all(event.socket.state()==QAbstractSocket.ConnectedState for event in events))
            for _ in range(10):app.processEvents()
            def request(api,method,path,**kwargs):
                finished=[];api.request(method,path,lambda result,error:finished.append((result,error)),**kwargs)
                until(lambda:bool(finished));result,error=finished[0];assert error is None,str(error);return result
            def publish(index,mime):
                previous=clients[index]._seen;clips[index].setMimeData(mime)
                until(lambda:clients[index]._seen!=previous and clients[index]._pending is None)
            intake=dict(item_id=str(uuid4()),upload_id=str(uuid4()),name='Preparing original folder',kind='file',size=0)
            request(accounts[0].api,'POST','/v1/sync/intake',data={'items':[intake]})
            rows=request(accounts[1].api,'GET','/v1/sync/changes')['transfers']
            assert any(row['upload_id']==intake['upload_id'] and row['state']=='preparing' for row in rows)
            request(accounts[0].api,'DELETE','/v1/sync/intake/'+intake['upload_id'])
            print('PASS production CloudApi validates early shelf placeholder protocol before staging or hashing')
            mime=QMimeData();mime.setText('HTTP original text 中文\r\n');publish(0,mime)
            until(lambda:clips[1].mimeData().text()=='HTTP original text 中文\r\n')
            assert clips[0].mimeData().text()=='HTTP original text 中文\r\n'
            image=QImage(160,90,QImage.Format_ARGB32);image.fill(QColor(12,34,56,177))
            mime=QMimeData();mime.setImageData(image);publish(1,mime)
            until(lambda:clips[0].mimeData().hasImage())
            assert clips[0].mimeData().imageData().convertToFormat(QImage.Format_RGBA8888)==image.convertToFormat(QImage.Format_RGBA8888)
            if NATIVE:assert_native_paste('image')
            original=Path(directory)/'Original-photo.bin';original.write_bytes(bytes(range(256))*(32768))
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))]);publish(0,mime)
            until(lambda:clips[1].mimeData().hasUrls())
            received=Path(clips[1].mimeData().urls()[0].toLocalFile());assert received.name==original.name and received.read_bytes()==original.read_bytes()
            for _ in range(10):assert received.read_bytes()==original.read_bytes()
            if NATIVE:
                assert_native_paste('files',str(original))
                mime=QMimeData();mime.setText('native return 中文');publish(1,mime)
                until(lambda:clips[0].mimeData().text()=='native return 中文')
                for _ in range(10):assert_native_paste('text','native return 中文')
                # Native delayed notifications must not create another PUT.
                identity=clients[0]._seen
                deadline=time.monotonic()+1.2
                while time.monotonic()<deadline:app.processEvents();time.sleep(.01)
                assert clients[0]._pending is None and clients[0]._seen==identity
                print('PASS native system clipboard: local copy stays pasteable, native image/files/text reads, repeated paste, no echo')
            print('PASS production Qt CloudApi + real Worker/R2: text, screenshot pixels, 8 MiB original file, repeated paste')
            if os.environ.get('FD_QA_LATENCY_REPORT') and not NATIVE:
                import math,statistics
                timings={}
                for kind in ('text','link','image','file'):
                    samples=[]
                    for index in range(8):
                        mime=QMimeData();marker=uuid4().hex
                        if kind in ('text','link'):
                            text=('原文 '+marker+'\r\n') if kind=='text' else 'https://example.invalid/'+marker
                            mime.setText(text);ready=lambda text=text:clips[1].mimeData().text()==text
                        elif kind=='image':
                            picture=QImage(640,360,QImage.Format_ARGB32);picture.fill(QColor(20+index,34,56,177))
                            mime.setImageData(picture)
                            ready=lambda picture=picture:clips[1].mimeData().hasImage() and clips[1].mimeData().imageData()==picture
                        else:
                            path=Path(directory)/(marker+'.bin');body=bytes(range(256))*4096;path.write_bytes(body)
                            mime.setUrls([QUrl.fromLocalFile(str(path))])
                            def ready(path=path,body=body):
                                values=clips[1].mimeData().urls()
                                return bool(values and Path(values[0].toLocalFile()).name==path.name and Path(values[0].toLocalFile()).read_bytes()==body)
                        started=time.perf_counter();clips[0].setMimeData(mime);until(ready)
                        samples.append((time.perf_counter()-started)*1000)
                        until(lambda:clients[0]._pending is None and not clients[1]._fetching)
                    timings[kind]=dict(samples_ms=samples,mean_ms=statistics.mean(samples),p95_ms=sorted(samples)[math.ceil(.95*len(samples))-1])
                Path(os.environ['FD_QA_LATENCY_REPORT']).write_text(json.dumps(dict(scope='Qt production client + authenticated WebSocket + real local workerd/D1/R2; isolated clipboard, no physical cross-device network',timings=timings),indent=2),encoding='utf8')
                print('PASS measured text/link/640x360 screenshot/1MiB original file; no manual refresh or OS notifications')
        finally:
            for event in events:event.close()
            for client in clients:client.close()
            until(lambda:all(not client._jobs for client in clients))
            for account in accounts:account.api.close()
    if '--verify-output' in sys.argv:
        from app_version import APP_VERSION
        Path(sys.argv[sys.argv.index('--verify-output')+1]).write_text(json.dumps(dict(ok=True,frozen=bool(getattr(sys,'frozen',False)),version=APP_VERSION,
            original_bytes=True,screenshot_pixels=True,repeated_paste=True,early_shelf_placeholder=True)),encoding='utf8')
    return 0


def assert_native_paste(kind,expected=''):
    """Read through the actual OS API, independently of QClipboard's MIME."""
    if sys.platform=='darwin':
        from AppKit import NSPasteboard,NSPasteboardTypeString,NSPasteboardTypePNG,NSPasteboardTypeTIFF
        from Foundation import NSURL
        board=NSPasteboard.generalPasteboard()
        if kind=='text':assert str(board.stringForType_(NSPasteboardTypeString))==expected
        elif kind=='image':assert board.dataForType_(NSPasteboardTypePNG) or board.dataForType_(NSPasteboardTypeTIFF)
        else:
            paths=board.readObjectsForClasses_options_([NSURL],{'NSPasteboardURLReadingFileURLsOnlyKey':True})
            assert paths and str(paths[0].path())==expected
    else:
        import ctypes
        from ctypes import wintypes as w
        u=ctypes.WinDLL('user32',use_last_error=True);k=ctypes.WinDLL('kernel32',use_last_error=True)
        for name,args,result in (('OpenClipboard',[w.HWND],w.BOOL),('GetClipboardData',[w.UINT],w.HANDLE),
                                ('RegisterClipboardFormatW',[w.LPCWSTR],w.UINT),('CloseClipboard',[],w.BOOL)):
            function=getattr(u,name);function.argtypes=args;function.restype=result
        k.GlobalLock.argtypes=[w.HANDLE];k.GlobalLock.restype=ctypes.c_void_p
        k.GlobalUnlock.argtypes=[w.HANDLE];k.GlobalUnlock.restype=w.BOOL
        assert u.OpenClipboard(None)
        try:
            if kind=='text':
                handle=u.GetClipboardData(13);assert handle
                pointer=k.GlobalLock(handle);assert pointer
                try:assert ctypes.wstring_at(pointer)==expected
                finally:k.GlobalUnlock(handle)
            elif kind=='image':assert u.GetClipboardData(8) or u.GetClipboardData(17) or u.GetClipboardData(u.RegisterClipboardFormatW('PNG'))
            else:
                handle=u.GetClipboardData(15);assert handle
                shell=ctypes.WinDLL('shell32',use_last_error=True)
                shell.DragQueryFileW.argtypes=[w.HANDLE,w.UINT,w.LPWSTR,w.UINT];shell.DragQueryFileW.restype=w.UINT
                assert shell.DragQueryFileW(handle,0xffffffff,None,0)==1
                length=shell.DragQueryFileW(handle,0,None,0);buffer=ctypes.create_unicode_buffer(length+1)
                assert shell.DragQueryFileW(handle,0,buffer,len(buffer))==length
                assert Path(buffer.value)==Path(expected)
        finally:u.CloseClipboard()
if __name__=='__main__':raise SystemExit(main())

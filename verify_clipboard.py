"""Qt production transport against an isolated Worker; no system clipboard."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json,sys,tempfile,time
from uuid import uuid4
from pathlib import Path
from PySide6.QtCore import QObject,Signal,QMimeData,QUrl
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication
from cloud_api import CloudApi
from clipboard_sync import ClipboardSync

class Clipboard(QObject):
    dataChanged=Signal()
    def __init__(self):super().__init__();self.mime=QMimeData()
    def mimeData(self):return self.mime
    def setMimeData(self,value):self.mime=value;self.dataChanged.emit()
    def clear(self):self.setMimeData(QMimeData())
class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,origin,session):
        super().__init__();self.api=CloudApi(origin,allow_test_http=True);self.api.token=session['token'];self.device_id=session['device_id'];self.signed_in=True
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
        clips=[Clipboard(),Clipboard()];accounts=[Account(connection['origin'],connection[key]) for key in ('a','b')]
        events=[Events(),Events()];clients=[ClipboardSync(accounts[i],events[i],Path(directory)/str(i),clipboard=clips[i]) for i in range(2)]
        try:
            for _ in range(10):app.processEvents()
            def request(api,method,path,**kwargs):
                finished=[];api.request(method,path,lambda result,error:finished.append((result,error)),**kwargs)
                until(lambda:bool(finished));result,error=finished[0];assert error is None,str(error);return result
            intake=dict(item_id=str(uuid4()),upload_id=str(uuid4()),name='Preparing original folder',kind='file',size=0)
            request(accounts[0].api,'POST','/v1/sync/intake',data={'items':[intake]})
            rows=request(accounts[1].api,'GET','/v1/sync/changes')['transfers']
            assert any(row['upload_id']==intake['upload_id'] and row['state']=='preparing' for row in rows)
            request(accounts[0].api,'DELETE','/v1/sync/intake/'+intake['upload_id'])
            print('PASS production CloudApi validates early shelf placeholder protocol before staging or hashing')
            mime=QMimeData();mime.setText('HTTP original text 中文\r\n');clips[0].setMimeData(mime)
            until(lambda:clients[0]._pending is None);clients[1].refresh()
            until(lambda:clips[1].mimeData().text()=='HTTP original text 中文\r\n')
            image=QImage(160,90,QImage.Format_ARGB32);image.fill(QColor(12,34,56,177))
            mime=QMimeData();mime.setImageData(image);clips[1].setMimeData(mime)
            until(lambda:clients[1]._pending is None);clients[0].refresh();until(lambda:clips[0].mimeData().hasImage())
            assert clips[0].mimeData().imageData().convertToFormat(QImage.Format_RGBA8888)==image.convertToFormat(QImage.Format_RGBA8888)
            original=Path(directory)/'Original-photo.bin';original.write_bytes(bytes(range(256))*(32768))
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))]);clips[0].setMimeData(mime)
            until(lambda:clients[0]._pending is None);clients[1].refresh();until(lambda:clips[1].mimeData().hasUrls())
            received=Path(clips[1].mimeData().urls()[0].toLocalFile());assert received.name==original.name and received.read_bytes()==original.read_bytes()
            for _ in range(10):assert received.read_bytes()==original.read_bytes()
            print('PASS production Qt CloudApi + real Worker/R2: text, screenshot pixels, 8 MiB original file, repeated paste')
        finally:
            for client in clients:client.close()
            until(lambda:all(not client._jobs for client in clients))
            for account in accounts:account.api.close()
    if '--verify-output' in sys.argv:
        from app_version import APP_VERSION
        Path(sys.argv[sys.argv.index('--verify-output')+1]).write_text(json.dumps(dict(ok=True,frozen=bool(getattr(sys,'frozen',False)),version=APP_VERSION,
            original_bytes=True,screenshot_pixels=True,repeated_paste=True,early_shelf_placeholder=True)),encoding='utf8')
    return 0
if __name__=='__main__':raise SystemExit(main())

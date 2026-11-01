"""Separate synthetic editor/file producer. No FlickDock imports or shared callbacks."""
import json,os,sys
from pathlib import Path
if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS')!='Windows':
    raise RuntimeError('Ephemeral Windows CI only')
from PySide6.QtCore import Qt,QTimer,QMimeData,QUrl
from PySide6.QtGui import QImage,QDrag
from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLineEdit,QPushButton

folder=Path(sys.argv[1]);app=QApplication([])
window=QWidget();window.setWindowTitle('FlickDock isolated external producer');window.resize(480,260)
layout=QVBoxLayout(window);text=QLineEdit(window);text.setAccessibleName('Synthetic external editor');layout.addWidget(text)
for label in ('Copy original file','Copy original image'):
    button=QPushButton(label,window);layout.addWidget(button)
    def copied(_,label=label):
        value=json.loads((folder/'producer.json').read_text());mime=QMimeData()
        if label=='Copy original file':mime.setUrls([QUrl.fromLocalFile(value['file'])]);mime.setData('application/x-flickdock-qa-native-source',b'preserve-native-source')
        else:
            image=QImage(value['image']);assert not image.isNull();mime.setImageData(image)
            mime.setData('image/png',Path(value['image']).read_bytes())
        app.clipboard().setMimeData(mime)
    button.clicked.connect(copied)
drag=QPushButton('Drag original files',window);layout.addWidget(drag)
class DragSource:
    def __init__(self):self.active=False
    def press(self,event):
        if event.button()!=Qt.LeftButton:return
        value=json.loads((folder/'producer.json').read_text());mime=QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in value.get('files',[value['file']])])
        operation=QDrag(drag);operation.setMimeData(mime);operation.exec(Qt.CopyAction)
source=DragSource();drag.mousePressEvent=source.press
window.show();window.move(20,20)
seen=[None]
def command():
    try:value=json.loads((folder/'editor-command.json').read_text())
    except (OSError,ValueError):return
    if value['id']==seen[0]:return
    seen[0]=value['id']
    if value['action']=='text':text.setText(value['text']);text.setFocus();text.selectAll()
    elif value['action']=='read_editor':
        (folder/'paste-result.json').write_text(json.dumps({'id':value['id'],'text':text.text()}))
    elif value['action']=='inspect':
        mime=app.clipboard().mimeData()
        result=dict(id=value['id'],text=mime.text(),files=[url.toLocalFile() for url in mime.urls()],
                    formats=mime.formats(),image=bool(mime.hasImage()))
        (folder/'native-result.json').write_text(json.dumps(result))
    elif value['action']=='quit':app.quit()
    (folder/'editor-ack.json').write_text(json.dumps({'id':value['id']}))
timer=QTimer();timer.timeout.connect(command);timer.start(20)
raise SystemExit(app.exec())

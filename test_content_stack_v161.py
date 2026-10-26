"""Colored family and actual latest-five stack artwork at fractional DPI."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
import tempfile
from unittest.mock import patch
from PySide6.QtGui import QImage,QFont,QFontDatabase
from PySide6.QtWidgets import QApplication
from content_stack import StackTile,stack_refs,stack_pixmap
from flickdock_icons import content_color,file_pixmap,document_pixmap,export_svg_family
from shelf_content import ContentRepository

app=QApplication([])
QFontDatabase.addApplicationFont(str(Path(__file__).parent/'fonts/HarmonyOS_Sans_SC_Regular.ttf'))
app.setFont(QFont('HarmonyOS Sans SC',11))
def pixels(pm):return bytes(pm.toImage().constBits())
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory);repo=ContentRepository()
    refs=[]
    for extension in ('pdf','mp3','docx','xlsx','zip','mp4','txt'):
        path=root/('item.'+extension);path.write_bytes(b'fixture');refs.append(str(path))
    assert stack_refs(refs)==tuple(refs[-5:])
    assert stack_refs(refs)[-1]==refs[-1]
    assert len({content_color(Path(ref).suffix) for ref in refs})==len(refs)
    for ratio in (1,1.25,1.5,2):
        old=stack_pixmap(repo,refs[:-1],128,dpr=ratio)
        new=stack_pixmap(repo,refs,128,dpr=ratio)
        assert new.width()==round(128*ratio) and new.devicePixelRatio()==ratio
        assert pixels(new)!=pixels(old),'Newly received card must change visible artwork'
        assert pixels(new)==pixels(stack_pixmap(repo,refs[-5:],128,dpr=ratio))
        assert pixels(stack_pixmap(repo,[refs[-1]]*5,128,dpr=ratio))!=pixels(stack_pixmap(repo,[refs[-1]],128,dpr=ratio))
    stack_pixmap(repo,refs[:-1],160,dpr=1.25).save(str(Path(__file__).parent/'v161-stack.png'))
    tile=StackTile(motion=True);tile.resize(170,140);tile.show()
    tile.set_stack(repo,refs[:-1]);tile.landing.setCurrentTime(120)
    midpoint=tile.arrival
    tile.set_stack(repo,refs)
    assert tile.refs[-1]==refs[-1] and 0<tile.arrival<=.8
    tile.landing.setCurrentTime(260);assert tile.arrival==1
    tile.set_stack(repo,refs);assert tile.arrival==1,'Repeated refresh must not restart landing'
    tile.set_stack(repo,[]);tile.set_content('empty');assert tile.refs==()
    image_path=root/'cached.png';image=QImage(300,200,QImage.Format.Format_RGB32);image.fill(0xff1689a8);image.save(str(image_path))
    with patch.object(repo,'item_pixmap',wraps=repo.item_pixmap) as provider:
        tile.set_stack(repo,[str(image_path)])
        for moment in (30,70,110,150,190,240):
            tile.landing.setCurrentTime(moment);tile.grab()
        assert provider.call_count==1,'Landing frames must reuse decoded artwork instead of reopening images'
    tile.close()
    export_svg_family(Path(__file__).parent/'assets/icons')
print('CONTENT_STACK_V161_OK latest on top/five maximum/distinct colors/four DPI scales')

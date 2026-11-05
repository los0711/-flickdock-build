"""Expanded formats, owned cover decoding, real Qt geometry and source actions."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import base64,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from PySide6.QtCore import QByteArray,QBuffer,QIODevice,QPoint,QRect,Qt
from PySide6.QtGui import QColor,QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QComboBox,QPushButton,QWidget
from app_settings import SettingsStore
from native_ui_common import configure_reference_font
from video_controller import VideoController
from video_covers import normalize_cover,MAX_COVER_BYTES
from video_formats import media_options
from video_panel import VideoDialog

APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)
configure_reference_font(APP)

def entry_fixture():
    entry=dict(id='visual-fixture',url='https://example.com/movie',title='沿海公路，开往山海之间',uploader='界面验证样本',duration=522,
        key=uuid4().hex,sources=[],cover=QImage(320,180,QImage.Format_RGB32),cover_state='ready',cover_error='',thumbnail_url='')
    entry['cover'].fill(QColor('#456979'))
    for index,(label,kind,detail,size) in enumerate((('2160p','video','MP4 · H.265 · 30 fps',482000000),('1440p','video','MP4 · H.264 · 30 fps',286000000),('1080p','video','MP4 · H.264 · 30 fps',164000000),('720p','video','MP4 · H.264 · 30 fps',86200000),('原始音频','audio','M4A · AAC · 192 kbps',12400000),('原始音频','audio','WEBM · OPUS · 128 kbps',12100000))):
        option=dict(id=str(index),label=label,kind=kind,details=detail,bytes=size,approximate=False)
        entry['sources'].append(dict(key=uuid4().hex,entry=entry,option=option,status='ready',progress=None,received=0,total=None,speed=None,path=None,job=None,actions=set(),shelved=False,error=''))
    return entry

class VideoFormatTests(unittest.TestCase):
    def test_every_native_resolution_codec_and_audio_source_is_exposed(self):
        from yt_dlp import YoutubeDL
        formats=[]
        for code,height,codec in (('137',1080,'avc1.640028'),('400',1440,'av01.0.12M.08'),('401',2160,'av01.0.12M.08')):
            formats.append(dict(format_id=code,height=height,fps=60,vcodec=codec,acodec='none',ext='mp4',filesize=100000,protocol='https',url='https://example.com/'+code))
        for code,ext,codec in (('140','m4a','mp4a.40.2'),('251','webm','opus')):
            formats.append(dict(format_id=code,vcodec='none',acodec=codec,abr=192,ext=ext,filesize=10000,protocol='https',url='https://example.com/'+code))
        with YoutubeDL({'quiet':True}) as ydl:options=media_options(dict(formats=formats,duration=10),ydl)
        self.assertEqual(len(options),5);self.assertEqual({row['id'] for row in options},{'v:137','v:400','v:401','a:140','a:251'})
        self.assertEqual({row['label'] for row in options if row['kind']=='video'},{'1080p','1440p','2160p'})
        self.assertTrue(all(row['bytes'] and row['details'] for row in options))

    def test_cover_is_normalized_and_non_raster_or_oversized_data_is_rejected(self):
        image=QImage(160,90,QImage.Format_RGB32);image.fill(QColor('#456979'));encoded=QByteArray();buffer=QBuffer(encoded);buffer.open(QIODevice.WriteOnly);image.save(buffer,'PNG');buffer.close()
        result=normalize_cover(bytes(encoded));decoded=QImage.fromData(base64.b64decode(result['image']))
        self.assertEqual(decoded.size(),image.size())
        for value in (b'<svg xmlns="http://www.w3.org/2000/svg"/>',b'x'*(MAX_COVER_BYTES+1),b''):
            with self.subTest(size=len(value)),self.assertRaises(ValueError):normalize_cover(value)

class VideoMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-video-matrix-');self.root=Path(self.temp.name)
        self.owner=QWidget();self.events=[];self.owner.add_paths=lambda mode,paths:self.events.append((mode,paths))
        self.settings=SettingsStore(self.root/'profile');self.settings.update(theme='dark',video_download_dir=str(self.root/'downloads'))
        self.manager=SimpleNamespace(app=APP,shelf=self.owner,settings=self.settings,motion=False)
        self.controller=VideoController(self.manager);self.entry=entry_fixture();self.controller.entries=[self.entry]
        self.screen=patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,1920,1080));self.screen.start()
        self.dialog=VideoDialog(self.manager,self.controller);self.dialog.show();APP.processEvents()
    def tearDown(self):
        self.controller.close();self.dialog.hide();self.dialog.deleteLater();self.owner.deleteLater();APP.processEvents();self.screen.stop();self.temp.cleanup()

    def test_all_sources_have_three_persistent_icon_actions_and_no_dropdown(self):
        self.assertEqual(len(self.dialog.cards),6);self.assertFalse(self.dialog.findChildren(QComboBox))
        self.assertTrue(self.dialog.parse_button.icon().isNull());self.assertEqual(self.dialog.parse_button.text(),'解析')
        if sys.platform=='darwin':
            self.assertEqual(len(self.dialog.traffic_buttons),3)
            self.assertTrue(all(control.isVisible() for control in self.dialog.traffic_buttons))
        else:self.assertEqual(set(self.dialog.chrome.buttons),{'min','max','close'})
        for card in self.dialog.cards.values():
            self.assertEqual(len(card.findChildren(QPushButton)),3)
            self.assertEqual([button.text() for button in (card.download,card.shelf,card.folder)],['','',''])
            self.assertFalse(card.folder.isEnabled())
            self.assertTrue(card.download.isVisible() and card.shelf.isVisible() and card.folder.isVisible())
        result=self.dialog.results[self.entry['key']]
        for width in (980,760,600):
            self.dialog.resize(width,740);APP.processEvents()
            self.assertIn(result.columns,(1,2))
            for card in result.cards.values():
                self.assertLessEqual(card.geometry().right(),result.width())

    def test_title_bar_moves_window_and_existing_window_fade_is_reused(self):
        from ui_transitions import WindowFade
        self.assertEqual(len(self.dialog.findChildren(WindowFade)),1)
        start=self.dialog.pos();point=QPoint(330,22)
        QTest.mousePress(self.dialog.title_bar,Qt.LeftButton,pos=point)
        QTest.mouseMove(self.dialog.title_bar,point+QPoint(24,15))
        QTest.mouseRelease(self.dialog.title_bar,Qt.LeftButton,pos=point+QPoint(24,15));APP.processEvents()
        self.assertNotEqual(self.dialog.pos(),start)

    def test_parse_success_stays_inline_and_explicit_cover_closes_with_its_window(self):
        self.controller.parsed.emit(self.entry);APP.processEvents()
        self.assertIsNone(self.dialog.cover_preview)
        self.assertIsNone(self.dialog.parentWidget())
        self.assertFalse(self.dialog.windowFlags()&Qt.WindowStaysOnTopHint)
        self.dialog.results[self.entry['key']].cover.click();APP.processEvents()
        preview=self.dialog.cover_preview;self.assertTrue(preview.isVisible());self.assertIs(preview.image.image,self.entry['cover'])
        self.dialog.hide();APP.processEvents();self.assertFalse(preview.isVisible())
        self.controller.parsed.emit(self.entry);APP.processEvents();self.assertFalse(preview.isVisible())

    def test_completed_source_shelves_once_and_opens_its_actual_file(self):
        row=self.entry['sources'][0];path=self.root/'downloads'/'movie.mp4';path.parent.mkdir(exist_ok=True);path.write_bytes(b'completed-download-fixture')
        row.update(path=str(path),status='complete',received=path.stat().st_size);self.dialog.refresh()
        card=self.dialog.cards[row['key']];self.assertTrue(card.folder.isEnabled())
        with patch('video_controller.reveal_file') as reveal:
            card.folder.click();reveal.assert_called_once_with(str(path))
        card.shelf.click();self.controller.action(row,'shelf');self.assertEqual(self.events,[('permanent',[str(path)])])
        self.assertFalse(card.shelf.isEnabled());self.assertEqual(path.read_bytes(),b'completed-download-fixture')

if __name__=='__main__':unittest.main()

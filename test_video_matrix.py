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
    def test_proxy_resolved_public_domain_is_not_rejected_by_local_synthetic_dns(self):
        from video_worker import public_request
        url='https://www.youtube.com/watch?v=LXMlDlqCTR0'
        for scheme in ('http','https','socks4a','socks5h'):
            with self.subTest(scheme=scheme),patch('video_worker.socket.getaddrinfo',side_effect=AssertionError('Local DNS must not resolve proxy destinations')):
                public_request(url,proxies={'https':scheme+'://127.0.0.1:7890'})
        private=[(2,1,6,'',('198.18.0.9',443))]
        for proxies in ({},{'https':'socks5://127.0.0.1:7890'},{'https':'http://127.0.0.1:7890','no':'*'}):
            with self.subTest(proxies=proxies),patch('video_worker.socket.getaddrinfo',return_value=private):
                with self.assertRaisesRegex(ValueError,'本机或局域网'):public_request(url,proxies=proxies)
        for host in ('127.0.0.1','192.168.1.1','[::1]','localhost','router.local'):
            with self.subTest(host=host),self.assertRaises(ValueError):
                public_request('https://'+host+'/movie',proxies={'https':'http://127.0.0.1:7890'})

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
        result=self.dialog.results[self.entry['key']]
        self.assertEqual(result.source_stack.currentIndex(),0)
        for kind in ('video','audio'):
            result.source_tabs[kind].click();APP.processEvents()
            for card in self.dialog.cards.values():
                self.assertEqual(card.isVisible(),card.row['option']['kind']==kind)
        for card in self.dialog.cards.values():
            self.assertEqual(len(card.findChildren(QPushButton)),3)
            self.assertEqual([button.text() for button in (card.download,card.shelf,card.folder)],['','',''])
            self.assertFalse(card.folder.isEnabled())
            self.assertTrue(all(not widget.isHidden() for widget in (card.download,card.shelf,card.folder)))
        result.source_tabs['video'].click();APP.processEvents()
        for width in (980,760,600):
            self.dialog.resize(width,740);APP.processEvents()
            self.assertIn(result.columns,(1,2))
            for kind in ('video','audio'):
                result.source_tabs[kind].click();APP.processEvents()
                for card in result.cards.values():
                    if card.row['option']['kind']==kind:
                        self.assertTrue(card.isVisible())
                        self.assertLessEqual(card.geometry().right(),card.parentWidget().width())

    def test_footer_keeps_switch_by_label_and_change_location_visible(self):
        for width in (980,600):
            self.dialog.resize(width,740);APP.processEvents()
            label=self.dialog.prompt_label;switch=self.dialog.prompt;folder=self.dialog.folder;button=self.dialog.folder_button
            self.assertTrue(all(widget.isVisible() for widget in (label,switch,folder,button)))
            self.assertLessEqual(switch.x()-label.geometry().right(),10)
            self.assertGreaterEqual(button.width(),76)
            self.assertLess(folder.geometry().right(),button.x())

    def test_source_actions_put_folder_before_shelf(self):
        card=next(iter(self.dialog.cards.values()))
        self.assertLess(card.download.geometry().left(),card.folder.geometry().left())
        self.assertLess(card.folder.geometry().left(),card.shelf.geometry().left())
        self.assertEqual(card.folder.accessibleName(),'打开所在文件夹')
        self.assertEqual(card.shelf.accessibleName(),'下载后放入搁架')

    def test_source_buttons_follow_the_page_theme_even_before_global_theme_updates(self):
        import ui_theme
        from native_ui_common import colors
        card=next(iter(self.dialog.cards.values()))
        original=ui_theme.current_theme()
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme('dark' if theme=='light' else 'light')
                self.settings.update(theme=theme);self.dialog.refresh();APP.processEvents()
                for control in (card.download,card.folder,card.shelf):
                    self.assertEqual(control.property('referenceTheme'),theme)
                    self.assertIn('background:'+colors(theme)['control'],control.styleSheet())
                    image=control.grab().toImage();pixel=image.pixelColor(6,6)
                    self.assertEqual(pixel.name(),QColor(colors(theme)['control']).name(),(theme,pixel.name()))
        finally:ui_theme.set_theme(original)

    def test_source_button_feedback_is_visible_in_both_themes(self):
        from PySide6.QtGui import QEnterEvent
        from PySide6.QtCore import QEvent,QPointF
        card=next(iter(self.dialog.cards.values()));button=card.download
        for theme in ('light','dark'):
            self.settings.update(theme=theme);self.dialog.refresh();APP.processEvents()
            button.setEnabled(True);button.setDown(False)
            APP.sendEvent(button,QEvent(QEvent.Leave));APP.processEvents()
            def fill():return button.grab().toImage().pixelColor(6,6).name()
            normal=fill()
            APP.sendEvent(button,QEnterEvent(QPointF(6,6),QPointF(6,6),QPointF(6,6)));APP.processEvents()
            hover=fill();button.setDown(True);APP.processEvents();pressed=fill()
            self.assertNotEqual(normal,hover,(theme,normal,hover))
            self.assertNotEqual(hover,pressed,(theme,hover,pressed))
            button.setDown(False);APP.sendEvent(button,QEvent(QEvent.Leave));button.setEnabled(False);APP.processEvents()
            self.assertFalse(button.isEnabled());self.assertEqual(fill(),normal)

    def test_source_scroll_ends_at_visible_cards_after_switching_pages(self):
        from PySide6.QtTest import QTest
        entry=entry_fixture();template=entry['sources'][0]
        for index in range(24):
            row=dict(template,key=uuid4().hex,option=dict(template['option'],label=str(index)))
            entry['sources'].append(row)
        self.controller.entries=[entry];self.dialog.refresh();QTest.qWait(50)
        result=self.dialog.results[entry['key']]
        for width in (980,600):
            self.dialog.resize(width,740);QTest.qWait(30)
            for kind in ('video','audio','video'):
                result.source_tabs[kind].click();QTest.qWait(30)
                bar=self.dialog.scroll.verticalScrollBar();bar.setValue(bar.maximum());QTest.qWait(20)
                visible=[card for card in result.cards.values() if card.isVisible()]
                self.assertTrue(visible)
                bottom=max(card.mapTo(self.dialog.scroll.viewport(),card.rect().bottomLeft()).y() for card in visible)
                if bar.maximum():
                    self.assertGreaterEqual(bottom,self.dialog.scroll.viewport().height()-40,(width,kind,bottom))
                    self.assertLess(bottom,self.dialog.scroll.viewport().height())
                else:self.assertEqual(bar.value(),0)
        result.source_tabs['video'].click();QTest.qWait(30)
        old_height=result.source_stack.height();entry['sources'][0].update(status='queued',progress=None)
        self.dialog.refresh();QTest.qWait(30)
        self.assertGreater(result.source_stack.height(),old_height)

    def test_video_audio_tabs_keep_download_state_and_compact_cards_in_both_themes(self):
        result=self.dialog.results[self.entry['key']]
        audio=next(row for row in self.entry['sources'] if row['option']['kind']=='audio')
        audio.update(status='queued',progress=None)
        for theme in ('light','dark'):
            self.settings.update(theme=theme);self.dialog.refresh();APP.processEvents()
            self.assertEqual(int(result.source_tabs['video'].font().weight()),400)
            title_font,count_font=result.source_tabs['video'].text_fonts()
            from PySide6.QtGui import QFontMetricsF
            self.assertLess(QFontMetricsF(count_font).height(),QFontMetricsF(title_font).height())
            result.source_tabs['audio'].click();APP.processEvents()
            card=result.cards[audio['key']]
            self.assertTrue(card.isVisible());self.assertEqual(card.status.text(),'等待下载…')
            self.assertLessEqual(card.height(),125)
            result.source_tabs['video'].click();APP.processEvents()
            self.assertFalse(card.isVisible())
            result.source_tabs['audio'].click();APP.processEvents()
            self.assertTrue(card.isVisible());self.assertEqual(card.status.text(),'等待下载…')
            self.assertTrue(result.tabs_frame.rect().contains(result.source_tabs['audio'].geometry()))

    def test_source_card_keeps_metadata_actions_and_status_in_a_compact_row(self):
        row=self.entry['sources'][0];card=self.dialog.cards[row['key']]
        for theme in ('light','dark'):
            self.settings.update(theme=theme)
            for width in (980,600):
                self.dialog.resize(width,740);row.update(status='queued',progress=None,job=None)
                self.dialog.refresh();APP.processEvents()
                self.assertEqual(card.details.text(),'MP4 · H.265 · 30 fps · 459.7 MB')
                self.assertEqual(card.status.text(),'等待下载…')
                icon_pos=card.kind_frame.mapTo(card,QPoint(0,0))
                text_pos=card.title.mapTo(card,QPoint(0,0))
                status_pos=card.status.mapTo(card,QPoint(0,0))
                self.assertGreaterEqual(icon_pos.x(),10)
                self.assertLess(icon_pos.x()+card.kind_frame.width(),text_pos.x())
                self.assertGreater(status_pos.y(),text_pos.y())
                self.assertLessEqual(card.height(),125)
        row.update(status='error',error='测试下载失败的完整原因',job=None);self.dialog.refresh();APP.processEvents()
        self.assertEqual(card.status.text(),'下载失败')
        self.assertEqual(card.status.toolTip(),'测试下载失败的完整原因')
        row.update(status='downloading',progress=.52,job=object(),error='');self.dialog.refresh();APP.processEvents()
        self.assertEqual(card.progress_percent.text(),'52%')
        self.assertEqual(card.status.text(),'正在下载…')
        self.assertTrue(card.progress_row.isVisible())
        self.assertLess(card.progress.geometry().right(),card.progress_percent.geometry().left())
        self.assertLessEqual(abs(card.progress.geometry().center().y()-card.progress_percent.geometry().center().y()),1)
        row['job']=None

    def test_audio_only_result_starts_on_audio_without_hiding_sources(self):
        from video_panel import VideoResult
        entry=entry_fixture();entry['sources']=[row for row in entry['sources'] if row['option']['kind']=='audio']
        result=VideoResult(entry,self.controller,self.dialog.body)
        self.assertFalse(result.source_tabs['video'].isEnabled())
        self.assertTrue(result.source_tabs['audio'].isChecked())
        self.assertEqual(result.source_stack.currentIndex(),1)
        self.assertEqual(len(result.cards),2)
        result.deleteLater()

    def test_cover_has_visible_press_feedback(self):
        cover=self.dialog.results[self.entry['key']].cover
        cover.clearFocus();APP.processEvents()
        before=cover.grab().toImage().pixelColor(12,12)
        cover.setDown(True);APP.processEvents()
        pressed=cover.grab().toImage().pixelColor(12,12)
        cover.setDown(False)
        self.assertNotEqual(before,pressed)

    def test_title_bar_moves_window_and_existing_window_fade_is_reused(self):
        from ui_transitions import WindowFade
        self.assertEqual(len(self.dialog.findChildren(WindowFade)),1)
        start=self.dialog.pos();point=QPoint(330,22)
        QTest.mousePress(self.dialog.title_bar,Qt.LeftButton,pos=point)
        QTest.mouseMove(self.dialog.title_bar,point+QPoint(24,15))
        QTest.mouseRelease(self.dialog.title_bar,Qt.LeftButton,pos=point+QPoint(24,15));APP.processEvents()
        self.assertNotEqual(self.dialog.pos(),start)

    def test_skeleton_runs_only_during_visible_parse_and_respects_reduced_motion(self):
        skeleton=self.dialog.skeleton
        self.assertTrue(skeleton.isHidden());self.assertFalse(skeleton._pulse.isActive())
        skeleton._motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            self.controller.extracting=object();self.controller.changed.emit();APP.processEvents()
            self.assertTrue(skeleton.isVisible());self.assertTrue(skeleton._pulse.isActive())
            self.dialog.hide();APP.processEvents();self.assertFalse(skeleton._pulse.isActive())
            self.dialog.show();APP.processEvents();self.assertTrue(skeleton._pulse.isActive())
            self.controller.extracting=None;self.controller.changed.emit();APP.processEvents()
            self.assertTrue(skeleton.isHidden());self.assertFalse(skeleton._pulse.isActive())
        with patch('ui_transitions.motion_allowed',return_value=False):
            self.controller.extracting=object();self.controller.changed.emit();APP.processEvents()
            self.assertTrue(skeleton.isVisible());self.assertFalse(skeleton._pulse.isActive())
            self.controller.extracting=None;self.controller.changed.emit()

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

    def test_reparse_reuses_completed_source_without_starting_another_download(self):
        from PySide6.QtCore import QObject,Signal
        row=self.entry['sources'][0];path=self.root/'downloads'/'completed.mp4'
        path.parent.mkdir(exist_ok=True);path.write_bytes(b'completed fixture')
        row.update(path=str(path),status='complete',received=path.stat().st_size)
        metadata={key:self.entry[key] for key in ('id','url','title','uploader','duration','thumbnail_url')}
        metadata['options']=[dict(row['option'])]
        class ParseJob(QObject):
            finished=Signal(object)
            def __init__(self,request,parent):super().__init__(parent);self.request=request
            def start(self):self.finished.emit({'error':'','result':{'entries':[metadata]}})
            def cancel(self):pass
        with patch('video_controller.VideoJob',ParseJob),patch.object(self.controller,'_start_download') as download:
            self.controller.parse(self.entry['url']);current=self.controller.entries[0]['sources'][0]
            self.assertEqual(current['status'],'complete');self.assertEqual(current['path'],str(path))
            self.controller.action(current,'shelf');download.assert_not_called()
        self.assertEqual(self.events,[('permanent',[str(path)])])

    def test_missing_completed_file_is_reported_instead_of_silently_redownloaded(self):
        row=self.entry['sources'][0];row.update(path=str(self.root/'missing.mp4'),status='complete')
        with patch.object(self.controller,'_start_download') as download:
            self.controller.action(row,'shelf');download.assert_not_called()
        self.assertIn('不存在或已被移动',self.controller.message)

    def test_completed_download_is_reused_after_controller_restart(self):
        from shelf_journal import coordinator
        from account_store import atomic_json
        for _ in range(100):
            if self.controller._history_ready:break
            QTest.qWait(10)
        self.assertTrue(self.controller._history_ready)
        path=self.root/'downloads'/'remembered.mp4';path.parent.mkdir(exist_ok=True);path.write_bytes(b'fixture')
        original=self.entry['sources'][0]
        self.controller._remember_download(original,path,path.stat())
        self.assertTrue(coordinator(self.settings.base,atomic_json).flush(3))
        reopened=VideoController(self.manager);entry=entry_fixture();reopened.entries=[entry]
        try:
            for _ in range(100):
                if reopened._history_ready:break
                QTest.qWait(10)
            self.assertTrue(reopened._history_ready)
            row=entry['sources'][0]
            self.assertEqual(row['status'],'complete');self.assertEqual(row['path'],str(path))
            with patch.object(reopened,'_start_download') as download:
                reopened.action(row,'shelf');download.assert_not_called()
            self.assertEqual(self.events,[('permanent',[str(path)])])
        finally:reopened.close();reopened.deleteLater()

    def test_cancel_while_checking_history_does_not_start_a_download(self):
        row=self.entry['sources'][0];self.controller._history_ready=False
        with patch.object(self.controller,'_start_download') as download:
            self.controller.action(row,'shelf');self.controller.cancel(row)
            self.controller._history_loaded({},None);download.assert_not_called()

    def test_missing_download_after_restart_asks_before_redownloading(self):
        from shelf_journal import coordinator
        from account_store import atomic_json
        for _ in range(100):
            if self.controller._history_ready:break
            QTest.qWait(10)
        path=self.root/'downloads'/'removed.mp4';path.parent.mkdir(exist_ok=True);path.write_bytes(b'fixture')
        self.controller._remember_download(self.entry['sources'][0],path,path.stat())
        self.assertTrue(coordinator(self.settings.base,atomic_json).flush(3));path.unlink()
        reopened=VideoController(self.manager);entry=entry_fixture();reopened.entries=[entry]
        try:
            for _ in range(100):
                if reopened._history_ready:break
                QTest.qWait(10)
            row=entry['sources'][0];self.assertTrue(row.get('history_stale'))
            with patch.object(reopened,'_start_download') as download:
                reopened.action(row,'shelf');download.assert_not_called()
            self.assertIn('不存在或已被移动',reopened.message)
        finally:reopened.close();reopened.deleteLater()

if __name__=='__main__':unittest.main()

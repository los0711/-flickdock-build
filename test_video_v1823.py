"""Real yt-dlp/FFmpeg workers, Qt controls, cancellation and action ownership."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QObject,Signal
from PySide6.QtWidgets import QApplication,QWidget
from app_settings import SettingsStore,SettingsDialog
from video_jobs import VideoJob
from video_links import LinkPromptHistory,video_url,recognized_video,candidate_link
from video_panel import VideoController,VideoDialog

APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)

def until(predicate,seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        APP.processEvents()
        if predicate():return
        time.sleep(.003)
    raise AssertionError('Video task did not settle')


class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=tempfile.TemporaryDirectory(prefix='video-fixture-');root=Path(cls.fixture.name)
        tools=Path(__file__).parent/'assets/video-tools'/('ffmpeg.exe' if sys.platform=='win32' else 'ffmpeg')
        if not tools.is_file():raise unittest.SkipTest('Prepare pinned video assets first')
        result=subprocess.run([str(tools),'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=160x90:rate=10',
            '-f','lavfi','-i','sine=frequency=440','-t','1.2','-c:v','mpeg4','-q:v','8','-c:a','aac',str(root/'sample.mp4')],capture_output=True,timeout=20)
        if result.returncode:raise AssertionError(result.stderr.decode('utf-8','replace'))
        cls.payload=(root/'sample.mp4').read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def response(self,body=False):
                if self.path=='/page':data=b'<html><head><title>Fixture movie</title></head><body><video controls src="/sample.mp4"></video></body></html>';kind='text/html'
                else:data=cls.payload*(100 if self.path=='/slow.mp4' else 1);kind='video/mp4'
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.end_headers()
                if not body:return
                try:
                    for index in range(0,len(data),1024):
                        self.wfile.write(data[index:index+1024]);self.wfile.flush()
                        if self.path=='/slow.mp4':time.sleep(.03)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
            def do_HEAD(self):self.response()
            def do_GET(self):self.response(True)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.server.daemon_threads=True
        cls.server_thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.server_thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.server_thread.join(3);cls.fixture.cleanup()

    def test_url_boundary_supported_sites_and_prompt_repeat_window(self):
        from urllib.parse import urlunsplit
        for url in ('file:///C:/Windows/test',urlunsplit(('https','user:secret@example.com','/video','','')),'http://127.0.0.1/video','http://[::1]/video','http://192.168.1.2/video','http://localhost/video','https://example.com/\nvideo'):
            with self.subTest(url=url),self.assertRaises((ValueError,UnicodeError)):video_url(url)
        self.assertEqual(candidate_link('一个视频 https://x.com/user/status/1234567890。'),'https://x.com/user/status/1234567890')
        cancel=threading.Event()
        self.assertTrue(recognized_video('https://www.youtube.com/watch?v=BaW_jenozKc',cancel))
        self.assertTrue(recognized_video('https://x.com/real/status/1234567890',cancel))
        self.assertTrue(recognized_video('https://www.facebook.com/watch/?v=1234567890',cancel))
        self.assertFalse(recognized_video('https://example.com/article',cancel))
        clock=[10.];history=LinkPromptHistory(lambda:clock[0]);self.assertTrue(history.claim('https://x.com/u/status/1?utm_source=a'))
        self.assertFalse(history.claim('https://twitter.com/u/status/1?utm_source=b'))
        clock[0]+=121;self.assertTrue(history.claim('https://x.com/u/status/1'))

    def test_actual_qt_worker_extracts_generic_html_and_downloads_video_audio_without_overwrite(self):
        result=[];job=VideoJob(dict(action='extract',url=self.origin+'/page',fixture_http=True))
        try:
            job.finished.connect(result.append);job.start();until(lambda:bool(result))
            self.assertFalse(result[0]['error'],result);entry=result[0]['result']['entries'][0]
            self.assertEqual({row['id'] for row in entry['options']},{'best','audio'})
            with tempfile.TemporaryDirectory() as directory:
                paths=[]
                for option in ('best','best','audio'):
                    done=[];request=dict(action='download',url=entry['url'],id=entry['id'],option=option,folder=directory,token=__import__('uuid').uuid4().hex,fixture_http=True)
                    worker=VideoJob(request)
                    try:
                        worker.finished.connect(done.append);worker.start();until(lambda:bool(done),30)
                        self.assertFalse(done[0]['error'],done)
                        path=Path(done[0]['result']['path']);self.assertTrue(path.is_file());paths.append(path)
                    finally:worker.cancel();worker.deleteLater()
                self.assertNotEqual(paths[0],paths[1]);self.assertEqual(paths[0].read_bytes(),self.payload)
                self.assertEqual(paths[1].read_bytes(),self.payload);self.assertEqual(paths[2].suffix,'.m4a')
                self.assertFalse(list(Path(directory).glob('.flickdock-download-*')))
        finally:job.cancel();job.deleteLater();APP.processEvents()

    def test_controller_uses_actual_download_for_transfer_shelf_and_cleans_cancelled_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=SettingsStore(root/'profile');settings.update(video_download_dir=str(root/'downloads'),theme='dark')
            events=[];shelf=QWidget();shelf.add_paths=lambda mode,paths:events.append(('shelf',paths))
            manager=SimpleNamespace(app=APP,settings=settings,shelf=shelf,queue_transfer_paths=lambda mode,paths:events.append(('transfer',paths)))
            controller=VideoController(manager);dialog=VideoDialog(manager,controller)
            def factory(request,parent):return VideoJob(dict(request,fixture_http=True),parent)
            with patch('video_panel.VideoJob',side_effect=factory),patch('video_panel.video_url',side_effect=lambda value:value):
                try:
                    controller.parse(self.origin+'/page');until(lambda:controller.extracting is None)
                    self.assertEqual(len(controller.entries),1,controller.message)
                    row=controller.entries[0];controller.action(row,'transfer');controller.action(row,'shelf')
                    until(lambda:row['status'] in ('complete','error'),30)
                    self.assertEqual(row['status'],'complete',row.get('error'))
                    self.assertEqual({event[0] for event in events},{'shelf','transfer'})
                    self.assertEqual({event[1][0] for event in events},{row['path']})
                    self.assertTrue(Path(row['path']).is_file())
                    dialog.show();APP.processEvents();dialog.cards[row['key']].preview.click()
                    until(lambda:dialog.media._loaded or dialog.media._failed)
                    self.assertFalse(dialog.media._failed,dialog.media.status_label.text())
                    self.assertEqual(dialog.media.path,row['path']);self.assertTrue(dialog.media.audio_output.isMuted())
                    dialog.hide();APP.processEvents();self.assertEqual(dialog.media.path,'')
                    dialog.prompt.setChecked(False);self.assertFalse(settings.video_link_prompt)
                    reloaded=SettingsStore(root/'profile');self.assertFalse(reloaded.video_link_prompt)
                    controller.parse(self.origin+'/slow.mp4');until(lambda:controller.extracting is None)
                    row=controller.entries[-1];controller.action(row,'download')
                    until(lambda:row['received']>0);controller.cancel(row);until(lambda:row['job'] is None)
                    self.assertEqual(row['status'],'cancelled');self.assertFalse(list((root/'downloads').glob('.flickdock-download-*')))
                    dialog.show();APP.processEvents();self.assertFalse(dialog.cards[row['key']].choice.isEnabled()==False)
                finally:controller.close();dialog.hide();dialog.deleteLater();shelf.deleteLater();APP.processEvents()


if __name__=='__main__':unittest.main()

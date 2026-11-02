"""Actual preview worker terminals release owned originals; no OS clipboard."""
import gc,hashlib,tempfile,threading,unittest,weakref
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QObject,Signal,QBuffer,QIODevice
from PySide6.QtGui import QImage,QColor
from test_html_native_workspace import until,flush
from test_win_clipboard_io_v1825 import Fake,frame,ensure_owned_process_temp
import win_clipboard_cache as cache
from win_clipboard_native import capture
from win_clipboard_io import decode_frame
import clipboard_preview as preview

class Client(QObject):
    view_changed=Signal(object)

class PresenterLifetimeTests(unittest.TestCase):
    def setUp(self):
        ensure_owned_process_temp();self.temp=tempfile.TemporaryDirectory(prefix='fd-presenter-owner-')
        self.base=Path(self.temp.name).resolve(strict=True)/'originals'
        self.base_patch=patch.object(cache,'cache_base',return_value=self.base);self.base_patch.start()
        self.presenters=[];self.clients=[]
    def tearDown(self):
        for presenter in self.presenters:
            presenter.close();until(lambda:not presenter.jobs and presenter.pool.activeThreadCount()==0);presenter.state={}
        for client in self.clients:client.view={}
        for value in (*self.presenters,*self.clients):value.deleteLater()
        flush();self.presenters.clear();self.clients.clear();gc.collect()
        cache.schedule_cleanup();self.assertTrue(cache._IDLE.wait(2))
        self.base_patch.stop();self.temp.cleanup()
    def content(self,kind):
        if kind=='text':formats={13:('complete owned original text\0').encode('utf-16-le')}
        else:
            image=QImage(16,8,QImage.Format_RGB32);image.fill(QColor('#2465db'))
            output=QBuffer();output.open(QIODevice.WriteOnly);self.assertTrue(image.save(output,'PNG'));formats={'image/png':bytes(output.data())}
        provider=Fake(formats);provider.read_snapshot=lambda name:formats[name]
        cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance)
        try:result=capture(provider,4,cache=sink)
        finally:sink.close()
        with patch('win_clipboard_io.MAX_BYTES',4):sample=decode_frame(frame(result),cache=cap)
        self.assertTrue(sample['valid']);self.assertFalse(sample['sync_eligible'])
        return sample['local_content']
    def terminal(self,kind,outcome):
        content=self.content(kind);content_ref=weakref.ref(content);lease_ref=weakref.ref(content._lease)
        path=Path(content.parts[0]['path']);expected=content.parts[0]['sha256']
        client=Client();client.view=dict(value=(kind,dict(local_content=content)),revision=1,phase='local');self.clients.append(client)
        del content
        function='read_clip_text' if kind=='text' else 'read_clip_image';original=getattr(preview,function)
        entered=threading.Event();release=threading.Event()
        def work(*args):
            if outcome=='error':raise PermissionError(13,'synthetic owned preview error')
            if outcome in ('closed','stale'):
                entered.set()
                if not release.wait(3):raise AssertionError('Owned preview worker not released')
            return original(*args)
        try:
            with patch.object(preview,function,side_effect=work):
                presenter=preview.ClipboardPresenter(client);self.presenters.append(presenter)
                job_refs=[weakref.ref(job) for job in presenter.jobs];self.assertEqual(len(job_refs),1)
                if outcome in ('closed','stale'):
                    until(entered.is_set)
                    if outcome=='closed':presenter.close()
                    else:presenter._update(dict(value=None,revision=2,phase='local'))
                    release.set()
                until(lambda:not presenter.jobs and presenter.pool.activeThreadCount()==0)
                if outcome=='success':self.assertTrue(presenter.text_preview if kind=='text' else not presenter.thumbnail.isNull())
                elif outcome=='error' and kind=='text':self.assertEqual(presenter.text_error,'无法读取剪贴板预览')
                self.assertIsNotNone(content_ref());self.assertTrue(path.exists())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),expected)
        finally:release.set()
        presenter.close();presenter.state={};client.view={};gc.collect()
        self.assertTrue(all(reference() is None for reference in job_refs),'Completed done subscription still retains its worker')
        self.assertIsNone(content_ref(),'Completed preview worker still retains the original content')
        self.assertIsNone(lease_ref(),'Completed preview worker still retains its original lease')
        until(lambda:not path.exists())
    def test_text_success(self):self.terminal('text','success')
    def test_text_closed(self):self.terminal('text','closed')
    def test_text_stale(self):self.terminal('text','stale')
    def test_text_error(self):self.terminal('text','error')
    def test_image_success(self):self.terminal('image','success')
    def test_image_closed(self):self.terminal('image','closed')
    def test_image_stale(self):self.terminal('image','stale')
    def test_image_error(self):self.terminal('image','error')

if __name__=='__main__':unittest.main()

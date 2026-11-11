"""Offscreen translation tests; never access the real clipboard or provider."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app_settings import SettingsDialog, SettingsStore
from translation_api import TranslationApi
from translation_controller import TranslationController
from translation_languages import LANGUAGES, copied_text, detect_source

APP=QApplication.instance() or QApplication([])


class Clipboard(QObject):
    copy_observed=Signal(object)
    def set_local_observation(self, enabled):self.observing=enabled


class Account(QObject):
    account_changed=Signal()
    signed_in=True


class Api(QObject):
    completed=Signal(int,str,str)
    busy=False
    def __init__(self):super().__init__();self.calls=[]
    def translate(self,*args):self.calls.append(args);self.busy=True
    def cancel(self):self.busy=False


class Notice:
    def __init__(self,*_):self.visible=False
    def present(self):self.visible=True
    def dismiss(self):self.visible=False
    def supersede(self):self.visible=False


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TEMP'))
        self.store=SettingsStore(Path(self.temp.name))
        self.store.update(translation_enabled=True)
        self.manager=SimpleNamespace(app=APP,settings=self.store,clipboard_sync=Clipboard(),account=Account(),show_settings=lambda **kw:None)
        self.api=Api()
        self.controller=TranslationController(self.manager,api=self.api,detector=lambda text:'英语')
        self.notice_patch=patch('translation_notice.TranslationNotice',Notice);self.notice_patch.start()

    def tearDown(self):
        self.controller.close();self.controller.pool.waitForDone(3000)
        self.notice_patch.stop();self.temp.cleanup()

    def test_links_never_trigger_translation(self):
        for value in ['https://youtube.com/watch?v=abc','https://example.com/a?q=%E4%B8%AD',
                      'www.example.com','example.com/path','mailto:a@example.com','magnet:?xt=urn:btih:ab',
                      ' https://a.example/\nhttps://b.example/ ', '<https://example.com>']:
            with self.subTest(value=value):
                self.controller.observe(('text',{'text':value}))
                self.assertEqual(self.controller.text,'');self.assertIsNone(self.controller.notice)
                self.assertEqual(self.api.calls,[])

    def test_files_images_invalid_and_numeric_content_do_not_trigger(self):
        for value in [None,('files',{'paths':['a']}),('image',{'png':b'abc'}),('text',{'text':' 12345 '}),('text',{'text':None}),('text',{'text':'hi\0there'})]:
            self.assertIsNone(copied_text(value))

    def test_prose_containing_link_stays_text(self):
        value='Please review https://example.com before tomorrow.'
        self.assertEqual(copied_text(('text',{'text':value})),value)

    def test_copy_invites_but_does_not_call_api(self):
        self.controller.observe(('text',{'text':'Hello world, this is a test.'}))
        for _ in range(50):
            if self.controller.source=='英语':break
            QTest.qWait(10)
        self.assertTrue(self.controller.notice.visible)
        self.assertEqual(self.controller.source,'英语');self.assertEqual(self.api.calls,[])
        self.controller.translate()
        self.assertEqual(len(self.api.calls),1);self.assertEqual(self.api.calls[0][2],'zh')
        self.controller.translate();self.assertEqual(len(self.api.calls),1)

    def test_old_result_cannot_replace_new_copy_or_target(self):
        self.controller.observe(('text',{'text':'First source text.'}));self.controller.translate()
        generation=self.controller.generation
        self.controller.observe(('text',{'text':'Second source text.'}))
        self.api.completed.emit(generation,'旧译文','');self.assertEqual(self.controller.result,'')
        self.controller.translate();generation=self.controller.generation
        self.store.update(translation_target='ja')
        self.api.completed.emit(generation,'旧目标语言','');self.assertEqual(self.controller.result,'')
        self.assertEqual(self.controller.phase,'idle')

    def test_disable_cancels_and_remembers_target(self):
        self.controller.observe(('text',{'text':'Please translate this sentence.'}));self.controller.translate()
        self.store.update(translation_target='fr',translation_enabled=False)
        self.assertFalse(self.api.busy);self.assertFalse(self.controller.notice.visible)
        self.controller.observe(('text',{'text':'Another sentence.'}));self.assertEqual(self.controller.text,'')
        restored=SettingsStore(self.store.base)
        self.assertFalse(restored.translation_enabled);self.assertEqual(restored.translation_target,'fr')

    def test_link_supersedes_pending_text(self):
        self.controller.observe(('text',{'text':'A normal sentence.'}));self.controller.translate()
        generation=self.controller.generation
        self.controller.observe(('text',{'text':'https://example.com'}))
        self.api.completed.emit(generation,'不应出现','')
        self.assertFalse(self.controller.notice.visible);self.assertEqual(self.controller.result,'')

    def test_language_order_and_local_detector(self):
        self.assertEqual(LANGUAGES[:2],(('zh','简体中文','cn'),('zh-Hant','繁体中文','tw')))
        self.assertEqual(len(LANGUAGES),30)
        self.assertEqual(detect_source('Small details make everyday work feel effortless.'),'英语')
        self.assertEqual(detect_source('Bonjour, nous vous souhaitons une excellente journée.'),'法语')

    def test_settings_is_one_document_and_anchor_tracks_scroll(self):
        dialog=SettingsDialog(self.store);dialog.show();QTest.qWait(60)
        try:
            self.assertTrue(all(section.isVisible() for _,sections in dialog.setting_categories for section in sections))
            self.assertGreater(dialog.scroll.verticalScrollBar().maximum(),0)
            dialog.select_category('翻译');QTest.qWait(650)
            self.assertEqual(dialog.category_list.currentItem().text(),'翻译')
            self.assertGreater(dialog.scroll.verticalScrollBar().value(),0)
            dialog.scroll.verticalScrollBar().setValue(0)
            self.assertEqual(dialog.category_list.currentItem().text(),'常规')
            dialog.translation_switch.click();self.assertFalse(self.store.translation_enabled)
        finally:dialog.hide();dialog.deleteLater()

    def test_api_synchronous_error_clears_busy(self):
        def request(method,path,callback,**options):
            self.assertEqual(path,'/v1/translation');self.assertNotIn('key',options['data'])
            callback(None,SimpleNamespace(code='UNAUTHORIZED'));return 'finished'
        client=TranslationApi(SimpleNamespace(request=request,cancel=lambda _:None))
        errors=[];client.completed.connect(lambda generation,result,error:errors.append(error))
        client.translate(1,'A sentence to translate.','zh')
        self.assertFalse(client.busy);self.assertTrue(errors)

    def test_api_cancel_ignores_late_callback(self):
        callbacks=[]
        def request(method,path,callback,**options):callbacks.append(callback);return 'request'
        client=TranslationApi(SimpleNamespace(request=request,cancel=lambda _:None))
        results=[];client.completed.connect(lambda *args:results.append(args))
        client.translate(1,'Please translate this.','zh');client.cancel()
        callbacks[0](dict(translation='旧结果'),None);self.assertEqual(results,[])

    def test_owned_write_ack_does_not_read_mime_or_reinvite_on_delayed_observer(self):
        import hashlib
        value='已翻译的文字'
        self.controller.result=value
        self.controller._copy_digest=hashlib.sha256(value.encode()).digest()
        clipboard=SimpleNamespace(ownsClipboard=lambda:True,mimeData=lambda:(_ for _ in ()).throw(AssertionError('Must not read potentially foreign MIME')))
        with patch('translation_controller.QApplication.clipboard',return_value=clipboard):
            self.controller._confirm_owned_copy(self.controller.generation)
        self.assertEqual(self.controller.phase,'copied');self.assertIsNotNone(self.controller._copy_digest)
        self.controller.observe(('text',{'text':value}))
        self.assertIsNone(self.controller.notice);self.assertEqual(self.controller.phase,'copied')
        self.assertIsNone(self.controller._copy_digest)

    def test_owned_write_timeout_only_clears_observer_fence(self):
        self.controller.phase='copied';self.controller._copy_digest=b'pending-observer'
        self.controller._copy_failed()
        self.assertEqual(self.controller.error,'');self.assertIsNone(self.controller._copy_digest)

if __name__=='__main__':unittest.main()

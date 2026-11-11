"""Offscreen translation tests; never access the real clipboard or provider."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QWidget
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

    def test_translation_works_signed_out_and_account_change_does_not_cancel_it(self):
        self.manager.account.signed_in=False
        self.manager.show_settings=lambda **kw: self.fail('Translation must not open login')
        self.controller.observe(('text',{'text':'Translate this without an account.'}))
        self.controller.translate();self.assertEqual(len(self.api.calls),1)
        generation=self.controller.generation
        self.manager.account.account_changed.emit()
        self.api.completed.emit(generation,'无需账号的译文','')
        self.assertEqual(self.controller.result,'无需账号的译文')

    def test_public_translation_transport_is_independent_of_account_logout(self):
        self.manager.account.api=SimpleNamespace(origin='https://flickdock.cc.cd',cancel_all=lambda:None)
        controller=TranslationController(self.manager,detector=lambda text:'英语')
        try:
            self.assertIsNot(controller.api.api,self.manager.account.api)
            self.assertEqual(controller.api.api.origin,self.manager.account.api.origin)
            callbacks=[]
            def request(method,path,callback,**options):
                self.assertFalse(options['authenticated']);callbacks.append(callback);return 'public-request'
            with patch.object(controller.api.api,'request',side_effect=request):
                controller.text='No login is required.';controller.translate()
                self.manager.account.api.cancel_all();self.manager.account.account_changed.emit()
                callbacks[0]({'translation':'无需登录。'},None)
            self.assertEqual(controller.result,'无需登录。')
        finally:controller.close()

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
            notes=[label.text() for label in dialog.translation_language.parentWidget().findChildren(QLabel)]
            self.assertTrue(any('无需登录即可使用' in text for text in notes))
            self.assertFalse(any('需登录 FlickDock' in text for text in notes))
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
            self.assertFalse(options['authenticated'])
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

class TranslationInteractionTests(unittest.TestCase):
    def setUp(self):
        from translation_notice import TranslationNotice
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('TEMP'))
        self.store=SettingsStore(Path(self.temp.name))
        class Controller(QObject):
            changed=Signal()
            source='英语';text='A short sentence for the original content.'
            result='一句简短的译文。';error='';phase='ready';busy=False
            def translate(self):raise AssertionError('UI check must not call the provider')
            def copy_result(self):raise AssertionError('UI check must not write the clipboard')
        self.controller=Controller()
        self.motion=patch('intake_notice.motion_allowed',return_value=False);self.motion.start()
        self.notice=TranslationNotice(SimpleNamespace(app=APP,settings=self.store),self.controller)
        self.notice.present();APP.processEvents()

    def tearDown(self):
        self.notice.supersede();self.notice.deleteLater();APP.processEvents()
        self.motion.stop();self.temp.cleanup()

    def test_invitation_and_result_use_five_seconds_across_user_interactions(self):
        self.assertTrue(self.notice.timer.isActive())
        self.assertEqual(self.notice.timer.interval(),5000)
        self.controller.phase='loading';self.controller.busy=True;self.controller.result=''
        self.notice.refresh();self.assertFalse(self.notice.timer.isActive())
        self.controller.phase='ready';self.controller.busy=False;self.controller.result='完成后的译文'
        self.notice.refresh();self.assertEqual(self.notice.timer.interval(),5000)
        self.notice.show_languages();self.assertFalse(self.notice.timer.isActive())
        self.notice.language_menu.hide();self.assertEqual(self.notice.timer.interval(),5000)
        self.notice.toggle_content();self.assertFalse(self.notice.timer.isActive())
        self.notice._collapse_content();self.assertEqual(self.notice.timer.interval(),5000)
        self.controller.phase='copied';self.notice.refresh()
        self.assertEqual(self.notice.timer.interval(),5000)
        self.notice.timer.timeout.emit();self.assertFalse(self.notice.isVisible())
        self.controller.result='';self.controller.phase='idle';self.notice.present()
        self.assertEqual(self.notice.timer.interval(),5000)

    def test_result_arriving_during_or_after_expansion_never_covers_capsule(self):
        from intake_notice import PILL_HEIGHT_PX
        host_size=self.notice.size()
        for response_at in (60,420):
            with self.subTest(response_at=response_at),patch('intake_notice.motion_allowed',return_value=True):
                self.notice._set_expanded(False)
                self.notice._morph.setCurrentTime(self.notice._morph.duration())
                self.controller.result='';self.controller.phase='loading';self.controller.busy=True
                self.controller.changed.emit();self.notice.middle_button.click()
                self.notice._morph.setCurrentTime(response_at)
                self.controller.result='翻译完成后的较长内容。\n'*24
                self.controller.phase='ready';self.controller.busy=False;self.controller.changed.emit()
                for at in (0,30,100,220,420):
                    self.notice._morph.setCurrentTime(at)
                    capsule=self.notice.capsule_rect()
                    self.assertLessEqual(self.notice.editor.geometry().bottom(),capsule.bottom()-PILL_HEIGHT_PX-10)
                    self.assertGreaterEqual(self.notice.editor.y(),capsule.top()+14)
                    self.assertEqual(self.notice.size(),host_size)
                self.assertTrue(self.notice.editor.isVisible())
                self.assertIn(self.controller.result,self.notice.editor.toPlainText())

    def test_short_chooser_scrolls_and_reopens_at_top_without_resetting_target(self):
        self.store.update(translation_target='fr')
        self.notice.show_languages();APP.processEvents()
        menu=self.notice.language_menu;view=menu.findChild(QListWidget)
        self.assertLessEqual(menu.height(),280);self.assertEqual(view.count(),30)
        self.assertGreater(view.verticalScrollBar().maximum(),0)
        self.assertEqual(view.verticalScrollBar().value(),0)
        self.assertEqual(self.store.translation_target,'fr')
        point=view.viewport().rect().center()
        event=QWheelEvent(QPointF(point),QPointF(view.viewport().mapToGlobal(point)),QPoint(),QPoint(0,-120),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
        APP.sendEvent(view.viewport(),event)
        self.assertGreater(view.verticalScrollBar().value(),0)
        view.setCurrentRow(29);view.scrollToItem(view.item(29));APP.processEvents()
        QTest.keyClick(view,Qt.Key_Return);APP.processEvents()
        self.assertEqual(self.store.translation_target,LANGUAGES[-1][0])
        self.assertIsNone(self.notice.language_menu)
        self.notice.show_languages();APP.processEvents()
        self.assertEqual(self.notice.language_menu.findChild(QListWidget).verticalScrollBar().value(),0)

    def test_flag_toggles_and_popup_outside_press_does_not_reopen_it(self):
        QTest.mouseClick(self.notice.flag_button,Qt.LeftButton);APP.processEvents()
        self.assertIsNotNone(self.notice.language_menu)
        QTest.mouseClick(self.notice.flag_button,Qt.LeftButton);APP.processEvents()
        self.assertIsNone(self.notice.language_menu)
        self.notice.show_languages();menu=self.notice.language_menu
        point=self.notice.flag_button.mapToGlobal(self.notice.flag_button.rect().center())
        event=QMouseEvent(QEvent.MouseButtonPress,QPointF(menu.mapFromGlobal(point)),QPointF(point),Qt.LeftButton,Qt.LeftButton,Qt.NoModifier)
        APP.sendEvent(menu,event)
        self.assertTrue(menu.testAttribute(Qt.WA_NoMouseReplay))
        APP.processEvents();self.assertIsNone(self.notice.language_menu)

    def test_outside_click_collapses_without_dismissing_or_consuming_other_control(self):
        self.notice.toggle_content();APP.processEvents()
        self.assertTrue(self.notice.expanded);self.assertTrue(self.notice.editor.isVisible())
        other=QWidget();other.move(0,0);other.resize(80,80);other.show()
        try:
            QTest.mouseClick(other,Qt.LeftButton,pos=QPoint(10,10))
            self.assertFalse(self.notice.expanded);self.assertTrue(self.notice.isVisible())
            self.assertFalse(self.notice._watching_content);self.assertFalse(self.notice.focus_timer.isActive())
        finally:other.hide();other.deleteLater();APP.processEvents()

    def test_existing_native_pointer_tick_catches_external_application_clicks(self):
        self.notice.toggle_content()
        outside=self.notice.geometry().topLeft()-QPoint(20,20)
        self.notice.reconcile_pointer(outside,0);self.notice.reconcile_pointer(outside,1)
        self.assertFalse(self.notice.expanded);self.assertTrue(self.notice.isVisible())

    def test_focus_loss_waits_two_seconds_and_focus_return_cancels_collapse(self):
        self.notice.toggle_content();APP.processEvents()
        with patch.object(self.notice,'_interaction_active',return_value=False):
            self.notice._focus_changed();self.assertEqual(self.notice.focus_timer.interval(),2000)
            QTest.qWait(1000);self.assertTrue(self.notice.expanded)
            QTest.qWait(1150);self.assertFalse(self.notice.expanded)
        self.notice.toggle_content()
        with patch.object(self.notice,'_interaction_active',return_value=False):self.notice._focus_changed()
        with patch.object(self.notice,'_interaction_active',return_value=True):
            self.notice._focus_changed();self.assertFalse(self.notice.focus_timer.isActive())
            self.notice._collapse_unfocused();self.assertTrue(self.notice.expanded)
        self.notice.supersede()
        self.assertFalse(self.notice._watching_content);self.assertFalse(self.notice.focus_timer.isActive())

if __name__=='__main__':unittest.main()

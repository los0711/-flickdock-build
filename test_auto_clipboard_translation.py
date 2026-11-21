"""Clipboard translation regressions with a fake provider and an isolated UI."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

from app_settings import SettingsStore
from test_translation import APP, Api, Clipboard
from translation_controller import TranslationController
from translation_focus import avoid_input
from translation_notice import TranslationNotice
from translator_speech import TranslatorSpeech
from test_breeze_speech_v1843 import FakeSpeechApi
from test_translator_v1843 import settle
import test_translator_v1844 as fixtures


class AutomaticClipboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.settings=SettingsStore(Path(self.temp.name))
        self.settings.update(translation_enabled=False,translation_rules={'en':'ja'})
        self.manager=SimpleNamespace(app=APP,settings=self.settings,clipboard_sync=Clipboard())
        self.api=Api()
        self.stamp=1
        self.controller=TranslationController(self.manager,api=self.api,detector=lambda _:'英语',sequence=lambda:self.stamp)

    def tearDown(self):
        self.controller.close();self.controller.pool.waitForDone(2000)
        self.controller.deleteLater();APP.processEvents();self.temp.cleanup()

    def observe(self, text):
        self.controller.observe(('text',{'text':text,'_translation_sequence':self.stamp}))

    def test_rule_and_writeback_are_one_operation(self):
        self.assertFalse(self.manager.clipboard_sync.observing)
        self.observe('Before enabling the editor button.')
        self.assertFalse(self.api.calls)
        self.controller.set_automatic(True,target='fr')
        self.assertTrue(self.manager.clipboard_sync.observing)
        self.observe('This is a sample text.')
        self.assertTrue(settle(lambda:len(self.api.calls)==1))
        generation,_,target=self.api.calls[0]
        self.assertEqual(target,'ja')
        self.assertIsNone(self.controller.notice)
        values=[]
        def write(mime):
            values.append(mime.text())
            self.stamp+=1;self.observe(mime.text())
        fixture=SimpleNamespace(setMimeData=write,ownsClipboard=lambda:True)
        with patch('PySide6.QtWidgets.QApplication.clipboard',return_value=fixture):
            self.api.completed.emit(generation,'日本語のサンプルです。','')
            APP.processEvents()
        self.assertEqual(values,['日本語のサンプルです。'])
        self.assertEqual(len(self.api.calls),1)
        self.assertEqual(self.controller.phase,'copied')

    def test_new_sequence_wins_before_native_notification_arrives(self):
        self.controller.set_automatic(True,target='ja')
        self.observe('This is a sample text.')
        self.assertTrue(settle(lambda:bool(self.api.calls)))
        self.stamp+=1
        with patch('PySide6.QtWidgets.QApplication.clipboard') as clipboard:
            self.api.completed.emit(self.api.calls[-1][0],'Obsolete result','')
        clipboard.assert_not_called()
        self.assertEqual(self.controller.phase,'idle')

    def test_target_change_invalidates_inflight_and_next_copy_uses_editor_language(self):
        self.settings.update(translation_rules={})
        self.controller.set_automatic(True,target='ja')
        self.observe('This is a sample text.')
        self.assertTrue(settle(lambda:bool(self.api.calls)))
        first=self.api.calls[0][0]
        self.controller.set_automatic(True,target='fr')
        with patch('PySide6.QtWidgets.QApplication.clipboard') as clipboard:
            self.api.completed.emit(first,'Obsolete result','')
        clipboard.assert_not_called()
        self.observe('Next sample.')
        self.assertTrue(settle(lambda:len(self.api.calls)==2))
        self.assertEqual(self.api.calls[-1][2],'fr')

    def test_same_language_and_private_data_are_skipped(self):
        self.settings.update(translation_rules={})
        self.controller.set_automatic(True,target='zh')
        self.observe('这里原本就是中文。')
        self.assertTrue(settle(lambda:self.controller.source=='中文'))
        self.controller.observe(('text',{'text':'private sample','_history_private':True}))
        self.assertIsNone(self.controller.notice)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.controller.phase,'idle')

    def test_disable_cancels_and_ignores_the_old_reply(self):
        self.controller.set_automatic(True,target='ja')
        self.observe('This is a sample text.')
        self.assertTrue(settle(lambda:bool(self.api.calls)))
        self.controller.set_automatic(False)
        self.assertFalse(self.manager.clipboard_sync.observing)
        with patch('PySide6.QtWidgets.QApplication.clipboard') as clipboard:
            self.api.completed.emit(self.api.calls[-1][0],'Old reply','')
        clipboard.assert_not_called()

    def test_auto_accepts_plain_text_excluded_from_normal_invitations(self):
        self.controller.set_automatic(True,target='ja')
        self.observe('12345')
        self.assertTrue(settle(lambda:len(self.api.calls)==1))
        self.assertEqual(self.api.calls[-1][1],'12345')
        self.observe('https://example.test/')
        self.assertTrue(settle(lambda:len(self.api.calls)==2))
        self.assertEqual(self.api.calls[-1][1],'https://example.test/')

    def test_long_copy_writes_only_the_complete_translation_and_preserves_gaps(self):
        self.controller.set_automatic(True,target='ja')
        text=('Example sentence. '*600).rstrip()+'\n\n'+('Second paragraph. '*400).rstrip()
        self.observe(text)
        self.assertTrue(settle(lambda:len(self.api.calls)==1))
        values=[]
        fixture=SimpleNamespace(setMimeData=lambda mime:values.append(mime.text()),ownsClipboard=lambda:True)
        with patch('PySide6.QtWidgets.QApplication.clipboard',return_value=fixture):
            generation=self.api.calls[-1][0]
            self.api.completed.emit(generation,'前半の訳。','')
            self.assertEqual(values,[]);self.assertEqual(len(self.api.calls),2)
            self.api.completed.emit(generation,'後半の訳。','')
            self.assertEqual(values,['前半の訳。\n\n後半の訳。'])


class RuleAndLanguageTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.NativeFixTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_editor_button_owns_automatic_scope_and_close_cancels_pending(self):
        from translator_panel import TranslatorDialog
        from test_translator_v1843 import FakeApi
        view=self.fixture.view
        manager=view.manager
        manager.app=APP
        manager.clipboard_sync=Clipboard()
        automatic=Api()
        controller=TranslationController(manager,api=automatic,detector=lambda _:'英语')
        manager.translation=controller
        from translator_store import TranslatorStore
        store=TranslatorStore(manager.settings.base,autoload=False)
        dialog=TranslatorDialog(manager,api=FakeApi(),store=store,detector=lambda _:'英语')
        try:
            dialog.show()
            dialog.session.set_language('target','ja')
            self.assertFalse(dialog.auto_clipboard_button.isChecked())
            self.assertEqual(dialog.source_editor.contextMenuPolicy(),Qt.DefaultContextMenu)
            self.assertEqual(dialog.output_editor.contextMenuPolicy(),Qt.DefaultContextMenu)
            dialog.auto_clipboard_button.click()
            self.assertTrue(controller.automatic)
            self.assertTrue(dialog.isVisible())
            manager.clipboard_sync.copy_observed.emit(('text',{'text':'A copied sample.'}))
            self.assertTrue(settle(lambda:bool(automatic.calls),1.5),
                            (controller.automatic,controller.phase,controller.source,controller.target,
                             dialog.session.target,dialog.isVisible(),controller.generation))
            self.assertEqual(automatic.calls[-1][2],'ja')
            dialog.close()
            self.assertFalse(controller.automatic)
            self.assertFalse(dialog.auto_clipboard_button.isChecked())
            with patch('PySide6.QtWidgets.QApplication.clipboard') as clipboard:
                automatic.completed.emit(automatic.calls[-1][0],'Old reply','')
            clipboard.assert_not_called()
            dialog.show()
            self.assertFalse(dialog.auto_clipboard_button.isChecked())
            dialog.auto_clipboard_button.click();dialog.auto_clipboard_button.click()
            self.assertFalse(controller.automatic)
        finally:
            dialog.shutdown();dialog.deleteLater();controller.close()
            controller.pool.waitForDone(2000);APP.processEvents()

    def test_rule_dialog_saves_and_rejects_another_rule_for_the_same_source(self):
        view=self.fixture.view;settings=view.manager.settings
        self.addCleanup(settings.preference_writer().close)
        view.rules_button.click()
        editor=view._managers['rules']
        self.assertTrue(editor.isVisible())
        editor._set_code('target','fr');editor.save.click()
        self.assertTrue(settle(lambda:editor.selected_source=='en'))
        self.assertEqual(settings.translation_rules,{'en':'fr'})
        editor._new();editor._set_code('target','ja');editor.save.click()
        self.assertIn('已有规则',editor.message.text())
        self.assertEqual(settings.translation_rules,{'en':'fr'})
        editor.list.setCurrentRow(0);editor._set_code('target','ja');editor.save.click()
        self.assertTrue(settle(lambda:not editor._saving))
        self.assertEqual(SettingsStore(settings.base).translation_rules,{'en':'ja'})
        editor.remove.click()
        self.assertTrue(settle(lambda:not editor._saving))
        self.assertEqual(settings.translation_rules,{})

    def test_management_dialogs_hide_the_outer_scrollbar(self):
        view=self.fixture.view
        self.addCleanup(view.manager.settings.preference_writer().close)
        for kind in ('glossary','rules','presets'):
            view.show_manager(kind)
            manager=view._managers[kind]
            APP.processEvents()
            self.assertEqual(manager.scroll.verticalScrollBarPolicy(),Qt.ScrollBarAlwaysOff)
            self.assertFalse(manager.scroll.verticalScrollBar().isVisible())
            manager.hide()

    def test_translator_uses_default_rule_and_respects_manual_choice(self):
        view=self.fixture.view
        view.manager.settings.update(translation_rules={'en':'ja','zh':'en'})
        state=view.session
        state.set_language('source','en')
        state.translate()
        self.assertEqual(state.target,'ja')
        state.set_language('target','fr');state.translate()
        self.assertEqual(state.target,'fr')

    def test_audio_catalog_and_polish_follow_the_pane_content(self):
        view=self.fixture.view;state=view.session
        state.source='zh';state.target='en'
        state.source_text='Please send your order confirmation with the delivery date.'
        state.result='请把订单确认和交付日期一起发过来。'
        view.show_tts('source');self.assertEqual(self.fixture.voice.voices[-1][1],'en')
        view.show_tts('target');self.assertEqual(self.fixture.voice.voices[-1][1],'zh')
        state.polish('精简',scope='source')
        sent=self.fixture.api.calls[-1]
        self.assertEqual(sent['target'],'en')
        self.assertIn('输入语言：英语',sent['instruction'])
        self.fixture.api.finish('Please confirm the order and delivery date.')
        self.assertTrue(state.source_unconfirmed)

    def test_mixed_provider_catalog_never_uses_other_languages_global_default(self):
        api=FakeSpeechApi();speech=TranslatorSpeech(api=api)
        try:
            speech.speak('English sentence.','en')
            token,_=api.voices[-1]
            api.voices_ready.emit(token,dict(voices=[
                dict(voice_id='other',name='Wrong',language_code='zh'),
                dict(voice_id='english',name='English',language_code='en-US'),
            ],default_available=True),'')
            self.assertEqual(api.calls[-1]['voice'],'english')
            self.assertEqual([v['key'] for v in speech.catalog_for('en')],['english'])
        finally:speech.close()


class NoticeTests(unittest.TestCase):
    def test_avoid_input_moves_to_side_if_the_center_is_blocked(self):
        area=QRect(0,0,1200,800);point=QPoint(400,450);bubble=QRect(70,220,260,44)
        obstacle=QRect(380,0,450,780)
        target=avoid_input(point,area,bubble,obstacle)
        self.assertNotEqual(target,point)
        self.assertTrue(area.contains(bubble.translated(target)))
        self.assertFalse(obstacle.intersects(bubble.translated(target)))

    def test_dragging_down_dismisses_without_opening_translator(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=SettingsStore(Path(directory))
            manager=SimpleNamespace(app=APP,settings=settings)
            controller=TranslationController(SimpleNamespace(app=APP,settings=settings,clipboard_sync=Clipboard()),
                                             api=Api(),detector=lambda _:'英语')
            notice=TranslationNotice(manager,controller)
            try:
                notice.present();QTest.qWait(20)
                button=notice.middle_button;point=QPointF(button.mapToGlobal(QPoint(10,10)))
                press=QMouseEvent(QEvent.MouseButtonPress,QPointF(10,10),point,Qt.LeftButton,Qt.LeftButton,Qt.NoModifier)
                move=QMouseEvent(QEvent.MouseMove,QPointF(10,42),point+QPointF(0,32),Qt.NoButton,Qt.LeftButton,Qt.NoModifier)
                notice.eventFilter(button,press)
                self.assertTrue(notice.eventFilter(button,move))
                self.assertTrue(notice._hiding)
                self.assertFalse(button.isDown())
            finally:notice.supersede();notice.deleteLater();controller.close();APP.processEvents()


if __name__=='__main__':unittest.main()

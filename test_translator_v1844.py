"""Sentence reuse, quote alignment, native controls and real timing regressions."""
from types import SimpleNamespace
from copy import deepcopy
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,Signal,QPoint,Qt,QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
from test_translator_v1843 import APP,FakeApi,settle
from test_breeze_speech_v1843 import FakeSpeechApi,FakePlayer,DATA
from translator_store import TranslatorStore
from translator_session import TranslatorSession
from translator_speech import TranslatorSpeech
from translation_document import TranslationDocument,split_units,utf16_position,python_position
from translation_alignment import SelectionAlignment


class UnitsApi(FakeApi):
    units_ready=Signal(int,object,str)
    aligned=Signal(int,object,str)
    def translate_units(self,epoch,units,target,**options):self.calls.append(dict(epoch=epoch,units=deepcopy(units),target=target,**options))
    def finish_units(self):
        call=self.calls[-1];self.units_ready.emit(call['epoch'],{'units':[dict(id=r['id'],text='译：'+r['text']) for r in call['units']]},'')
    def align(self,epoch,source,target,selection,side,begin):self.calls.append(dict(epoch=epoch,source=source,target=target,selection=selection,side=side,begin=begin))


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=TranslatorStore(self.temp.name,autoload=False);self.store.data['live']=False
        self.api=UnitsApi();self.session=TranslatorSession(self.api,self.store,detector=lambda text:'英语');self.session.source='en';self.session.target='zh'
    def tearDown(self):self.session.close();self.session.deleteLater();settle(timeout=.02);self.temp.cleanup()
    def translate(self,text):self.session.edit_source(text);self.session.translate();self.api.finish_units()
    def test_word_edit_only_sends_changed_sentence_and_keeps_other_translation(self):
        self.translate('First sentence. Second red sentence. Last sentence.');old=self.session.result
        self.session.edit_source('First sentence. Second blue sentence. Last sentence.');self.assertEqual(self.session.result,old)
        self.session.translate();rows=self.api.calls[-1]['units'];self.assertEqual([r['text'] for r in rows],['Second blue sentence.'])
        self.assertEqual(rows[0]['before'],'First sentence.');self.assertEqual(rows[0]['after'],'Last sentence.')
        self.api.finish_units();self.assertIn('译：First sentence.',self.session.result);self.assertNotIn('red',self.session.result)
    def test_insert_front_middle_end_and_delete_do_not_retranslate_existing_units(self):
        self.translate('A first sentence. A last sentence.')
        for text,new in [('New start. A first sentence. A last sentence.','New start.'),
                         ('New start. A first sentence. New middle. A last sentence.','New middle.'),
                         ('New start. A first sentence. New middle. A last sentence. New end.','New end.')]:
            self.session.edit_source(text);self.session.translate();self.assertEqual([r['text'] for r in self.api.calls[-1]['units']],[new]);self.api.finish_units()
        count=len(self.api.calls);self.session.edit_source('A first sentence. A last sentence.');self.session.translate()
        self.assertEqual(len(self.api.calls),count);self.assertNotIn('New',self.session.result)
    def test_failure_and_late_response_preserve_last_completed_document(self):
        self.translate('First sentence. Last sentence.');old=self.session.result
        self.session.edit_source('Changed sentence. Last sentence.');self.session.translate();epoch=self.session.epoch
        self.api.units_ready.emit(epoch,None,'Network failure');self.assertEqual(self.session.result,old)
        self.session.edit_source('Another sentence. Last sentence.');self.api.units_ready.emit(epoch,{'units':[dict(id='0',text='STALE')]},'')
        self.assertNotIn('STALE',self.session.result)
    def test_chinese_to_chinese_is_local_and_source_revisions_survive_auto_translation(self):
        self.session.source='zh';self.session.target='zh';self.session.edit_source('你好，世界。');self.session.translate()
        self.assertFalse(self.api.calls);self.assertEqual(self.session.result,'你好，世界。')
        self.session.source='en';self.session.target='zh';self.session.set_polish_scope('source');self.session.polish('精简')
        self.api.finish('Edited source.');self.session.translate();self.api.finish_units()
        self.assertEqual(len(self.session.source_revisions),1);self.session.undo('source');self.assertEqual(self.session.source_text,'你好，世界。')
    def test_large_document_submits_only_one_sentence_on_edit(self):
        text=' '.join(f'This is sentence number {i} with a delivery plan.' for i in range(100))
        self.translate(text);initial=sum(len(r['text']) for r in self.api.calls[-1]['units'])
        self.session.edit_source(text.replace('number 50 with','number 50 changed with'));self.session.translate()
        rows=self.api.calls[-1]['units'];self.assertEqual(len(rows),1)
        self.assertLess(sum(len(r['text'])+len(r['before'])+len(r['after']) for r in rows),initial*.05)
    def test_unicode_offsets_decimals_and_abbreviations(self):
        text='😀 Dr. Green costs 1.25 dollars. 下一句。\n'
        units=split_units(text);self.assertEqual(units[0].text,'😀 Dr. Green costs 1.25 dollars.')
        for position in range(len(text)+1):self.assertEqual(python_position(text,utf16_position(text,position)),position)


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.doc=TranslationDocument();self.doc.pair('😀 Please confirm the delivery date.','😀 请确认交付日期。')
        self.api=UnitsApi();self.alignment=SelectionAlignment(self.doc,self.api);self.out=[];self.alignment.highlight.connect(lambda *v:self.out.append(v))
    def tearDown(self):self.alignment.cancel();self.alignment.deleteLater();settle(timeout=.01)
    def test_partial_phrase_uses_exact_quote_and_cache_in_both_directions(self):
        source,target=self.doc.source,self.doc.target;begin=source.index('delivery date')
        self.alignment.select('source',begin,begin+13,source,target);self.alignment.timer.stop();self.alignment._request()
        self.api.aligned.emit(self.alignment.epoch,{'certain':True,'quote':'交付日期'},'')
        a=target.index('交付日期');self.assertEqual(self.out[-1],('target',[(a,a+4)]))
        count=len(self.api.calls);self.alignment.select('source',begin,begin+13,source,target);self.assertEqual(len(self.api.calls),count)
        self.alignment.select('target',a,a+4,source,target);self.alignment.timer.stop();self.alignment._request()
        self.api.aligned.emit(self.alignment.epoch,{'certain':True,'quote':'delivery date'},'');self.assertEqual(self.out[-1],('source',[(begin,begin+13)]))
    def test_ambiguous_or_stale_alignment_never_makes_up_character_positions(self):
        s,t=self.doc.source,self.doc.target;self.alignment.select('source',3,9,s,t);self.alignment.timer.stop();self.alignment._request();epoch=self.alignment.epoch
        expected=self.out[-1];self.api.aligned.emit(epoch,{'certain':True,'quote':'not in translation'},'');self.assertEqual(self.out[-1],expected)
        self.alignment.select('source',0,0,s,t);self.api.aligned.emit(epoch,{'certain':True,'quote':'交付日期'},'');self.assertEqual(self.out[-1],('target',[]))


class NativeFixTests(unittest.TestCase):
    def setUp(self):
        from app_settings import SettingsStore
        from translator_panel import TranslatorDialog
        from ui_transitions import WindowFade
        self.temp=tempfile.TemporaryDirectory();self.patch=patch.object(WindowFade,'enabled',return_value=False);self.patch.start()
        self.shelf=QWidget();self.store=TranslatorStore(self.temp.name,autoload=False);self.store.data['live']=False;self.api=FakeApi();self.voice=FakeSpeechApi()
        self.speech=TranslatorSpeech(api=self.voice,player_factory=lambda owner:(FakePlayer(owner),None))
        self.view=TranslatorDialog(SimpleNamespace(settings=SettingsStore(self.temp.name),shelf=self.shelf),api=self.api,store=self.store,speech=self.speech,detector=lambda text:'英语')
        self.view.show();self.view.source_editor.setPlainText('Hello world.');self.view.session.translate();self.api.finish('你好，世界。');settle(timeout=.02)
    def tearDown(self):self.view.shutdown();self.view.deleteLater();self.shelf.deleteLater();settle(timeout=.03);self.patch.stop();self.temp.cleanup()
    def test_real_close_button_stays_closed_after_refresh(self):
        self.view._speak(True);self.voice.finish_voice();self.voice.finish_audio();settle(timeout=.02)
        card=self.view.speech_bars['target'][0];self.assertTrue(card.isVisible())
        button=next(w for w in card.findChildren(QWidget) if w.accessibleName()=='停止并关闭朗读卡片')
        button.click();self.view.refresh();self.assertFalse(card.isVisible());self.assertFalse(self.speech.active)
    def test_source_compare_can_be_closed_and_target_scope_does_not_redirect_source_undo(self):
        self.view._set_polish_scope('source');self.view.session.polish('简短');self.api.finish('Hello.');settle(timeout=.02)
        self.assertTrue(self.view.source_comparison.isVisible());self.view.source_compare_button.click();self.view.refresh();self.assertFalse(self.view.source_comparison.isVisible())
        self.view._set_polish_scope('target');self.view.session.undo('source');self.assertEqual(self.view.session.source_text,'Hello world.')
    def test_delay_popup_has_no_mouse_replay_and_second_toggle_closes(self):
        self.view.delay_button.click();self.assertTrue(self.view.delay_popup.isVisible());self.assertTrue(self.view.delay_popup.testAttribute(Qt.WA_NoMouseReplay))
        self.view.delay_button.click();self.assertFalse(self.view.delay_popup.isVisible())
    def test_anchor_press_after_interacting_with_popup_is_consumed_once(self):
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QPointF
        self.view.show_delay();self.view.delay_slider.setValue(14)
        position=self.view.delay_button.mapToGlobal(QPoint(5,5))
        event=QMouseEvent(QEvent.MouseButtonPress,QPointF(5,5),QPointF(position),Qt.LeftButton,Qt.LeftButton,Qt.NoModifier)
        self.assertTrue(self.view.delay_popup.eventFilter(self.view.delay_button,event));self.assertFalse(self.view.delay_popup.isVisible())
    def test_audition_uses_voice_preview_and_loading_row_not_editor_text(self):
        self.view.speech.ensure_engine('en');self.voice.finish_voice();self.view._voices_changed();self.view._show_voices()
        self.view.voice_popup.rows[0][1].play.click();self.assertEqual(self.speech.side,'audition');self.assertNotIn('world',self.speech.text)
        self.assertTrue(self.view.voice_popup.rows[0][1].loading.isVisible());self.voice.finish_audio();settle(timeout=.02)
        self.assertFalse(self.view.voice_popup.rows[0][1].loading.isVisible());self.assertTrue(self.speech.active)
    def test_subtitle_obeys_real_timestamps_instead_of_character_percentage(self):
        self.view._speak(False);self.assertTrue(settle(lambda:bool(self.voice.voices),2));self.voice.finish_voice()
        value=dict(audio=DATA['audio'],timing=[dict(word='Hello',start=.0,end=.1),dict(word='world',start=.8,end=1.)],starts=[0,.8])
        self.voice.completed.emit(self.voice.calls[-1]['epoch'],value,'');settle(timeout=.02)
        self.speech.player.move(500);self.assertEqual(self.speech.subtitle(),('Hello world.',5))
        self.speech.player.move(900);self.assertEqual(self.speech.subtitle(),('Hello world.',11))
    def test_export_during_playback_uses_a_distinct_request_lane(self):
        download=FakeSpeechApi();self.voice.fork=lambda parent:download
        self.view._speak(True);self.voice.finish_voice();self.voice.finish_audio();settle(timeout=.02)
        count=len(self.voice.calls)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audio.mp3';self.speech.export_audio(path,'Another sample.','en')
            self.assertTrue(self.speech.active);self.assertTrue(self.speech.player.playing);self.assertEqual(len(self.voice.calls),count)
            download.finish_audio();self.assertTrue(settle(path.exists,2));self.assertTrue(self.speech.active)
    def test_generation_failure_is_not_presented_as_completed_reading(self):
        self.view._speak(True);self.voice.finish_voice();self.voice.finish_audio('Synthetic failure');settle(timeout=.02)
        self.assertFalse(self.speech.finished);self.assertFalse(self.view.speech_bars['target'][0].isVisible())
        self.view._speak(True);self.voice.finish_audio();settle(timeout=.02);self.speech.player.finish()
        self.assertTrue(self.speech.finished);self.assertTrue(self.view.speech_bars['target'][0].isVisible())
    def test_notice_entry_populates_real_translator_without_morphing_the_capsule(self):
        from flickdock import ShelfManager
        manager=self.view.manager;manager.translator_dialog=self.view;manager._note_ui=lambda event:None
        manager.shelf._release_reveal_level=lambda:None
        ShelfManager.show_translator_dialog(manager,text='Synthetic copied message.',target='ja')
        self.assertEqual(self.view.source_editor.toPlainText(),'Synthetic copied message.');self.assertEqual(self.view.session.target,'ja')


class InvitationTests(unittest.TestCase):
    def test_language_result_survives_target_change_and_middle_opens_populated_translator(self):
        import threading
        from app_settings import SettingsStore
        from translation_controller import TranslationController
        from test_translation import Clipboard,Account,Api,Notice
        gate=threading.Event();calls=[]
        with tempfile.TemporaryDirectory() as directory,patch('translation_notice.TranslationNotice',Notice):
            settings=SettingsStore(directory);settings.update(translation_enabled=True)
            manager=SimpleNamespace(app=APP,settings=settings,clipboard_sync=Clipboard(),account=Account(),show_translator_dialog=lambda **kw:calls.append(kw))
            def detector(text):gate.wait(2);return '中文'
            api=Api();controller=TranslationController(manager,api=api,detector=detector)
            try:
                controller.observe(('text',{'text':'这是合成的语言识别测试。'}));settings.update(translation_target='fr');gate.set()
                self.assertTrue(settle(lambda:controller.source=='中文',2))
                settings.update(translation_target='zh');self.assertEqual(controller.target,'en')
                controller.open_translator();self.assertEqual(calls[-1],dict(text='这是合成的语言识别测试。',target='en'))
                self.assertFalse(controller.notice.visible)
            finally:gate.set();controller.close();controller.pool.waitForDone(2500)
    def test_actual_detector_handles_synthetic_chinese_english_french(self):
        from translation_languages import detect_source
        for text,expected in [('这是用于识别语言的中文测试。','中文'),('Hello, please confirm your delivery schedule.','英语'),('Bonjour, merci pour votre réponse.','法语')]:self.assertEqual(detect_source(text),expected)


class ExportTests(unittest.TestCase):
    def test_export_lane_does_not_cancel_playback_and_atomic_save_preserves_bytes(self):
        from speech_export import SpeechExporter
        api=FakeSpeechApi();export=SpeechExporter(api);saved=[]
        try:
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'voice.mp3';path.write_bytes(b'previous')
                export.start(path,['one','two'],'en','voc_en',1.,1.,saved.append)
                api.finish_audio();self.assertEqual(path.read_bytes(),b'previous')
                api.finish_audio();self.assertTrue(settle(lambda:bool(saved),2))
                self.assertEqual(path.read_bytes(),DATA['audio']*2);self.assertFalse(list(Path(directory).glob('.flickdock-*')))
        finally:export.close();export.deleteLater()
    def test_cancelled_export_never_overwrites_existing_file(self):
        from speech_export import SpeechExporter
        api=FakeSpeechApi();export=SpeechExporter(api)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'voice.mp3';path.write_bytes(b'keep')
                export.start(path,['one'],'en','voc_en',1.,1.,None);epoch=export.epoch;export.cancel()
                api.completed.emit(epoch,dict(DATA),'');settle(timeout=.02);self.assertEqual(path.read_bytes(),b'keep')
        finally:export.close();export.deleteLater()


if __name__=='__main__':unittest.main(verbosity=2)

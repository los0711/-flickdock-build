"""Translator races, persistence and actual native controls; no real providers."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QPoint, QRect, Signal
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QWidget

from translation_api import TranslationApi
from translator_session import TranslatorSession
from translator_store import MAX_HISTORY, MAX_HISTORY_CHARS, TranslatorStore, defaults, normalize

APP=QApplication.instance() or QApplication([])


def settle(predicate=lambda:False,timeout=.08):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        APP.processEvents()
        if predicate():return True
        time.sleep(.002)
    return bool(predicate())


class FakeApi(QObject):
    completed=Signal(int,str,str)
    def __init__(self):super().__init__();self.calls=[];self.cancelled=0
    def translate(self,epoch,text,target,**values):self.calls.append(dict(kind='translate',epoch=epoch,text=text,target=target,**values))
    def polish(self,epoch,text,target,instruction,**values):self.calls.append(dict(kind='polish',epoch=epoch,text=text,target=target,instruction=instruction,**values))
    def cancel(self):self.cancelled+=1
    def finish(self,text,error=''):self.completed.emit(self.calls[-1]['epoch'],text,error)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=TranslatorStore(self.temp.name,autoload=False)
        self.store.data['live']=False;self.api=FakeApi();self.session=TranslatorSession(self.api,self.store,detector=lambda text:'英语')
    def tearDown(self):self.session.close();APP.processEvents();self.temp.cleanup()
    def translated(self):
        self.session.edit_source('A synthetic source.');self.session.translate();self.api.finish('基准译文。')
    def test_late_response_cannot_replace_new_input(self):
        self.translated();epoch=self.api.calls[-1]['epoch'];self.session.edit_source('New source')
        self.api.completed.emit(epoch,'过期内容','');self.assertEqual(self.session.result,'基准译文。')
        self.assertEqual(self.session.source_text,'New source')
    def test_chained_polish_uses_current_text_and_undo_preserves_original(self):
        self.translated();self.session.polish('更亲切');self.assertEqual(self.api.calls[-1]['text'],'基准译文。');self.api.finish('第一版。')
        self.session.polish('精简');self.assertEqual(self.api.calls[-1]['text'],'第一版。');self.api.finish('第二版。')
        self.assertEqual(self.session.original,'基准译文。');self.session.undo();self.assertEqual(self.session.result,'第一版。')
        self.session.restore_original();self.assertEqual(self.session.result,'基准译文。');self.assertFalse(self.session.revisions)
    def test_regenerate_failure_preserves_latest_success(self):
        self.translated();self.session.polish('商务');self.api.finish('商务版本。');self.session.regenerate()
        self.assertEqual(self.api.calls[-1]['text'],'基准译文。');self.api.finish('','网络异常')
        self.assertEqual(self.session.result,'商务版本。');self.assertEqual(len(self.session.revisions),1)
    def test_cancel_and_hide_fence_work_and_stop_debounce(self):
        self.store.data['live']=True;self.session.edit_source('Input before hiding')
        self.assertTrue(self.session.debounce.isActive());self.session.suspend()
        self.assertFalse(self.session.debounce.isActive());self.assertFalse(self.session.translate());self.assertFalse(self.api.calls)
        self.session.suspend(False);self.assertTrue(self.session.debounce.isActive())
    def test_manual_edit_and_replace(self):
        self.translated();self.session.edit_result('手动修改。');self.assertEqual(self.session.original,'基准译文。')
        self.session.replace_original();self.assertEqual(self.session.original,'手动修改。')
    def test_rapid_presets_only_accept_latest_completion(self):
        self.translated();self.session.polish('指令一');old=self.api.calls[-1]['epoch'];self.session.polish('指令二')
        self.api.completed.emit(old,'旧版本','');self.assertEqual(self.session.result,'基准译文。')
        self.api.finish('新版本');self.assertEqual(self.session.result,'新版本');self.assertEqual(len(self.session.revisions),1)
    def test_source_polish_preserves_language_scope_and_updates_input_not_output(self):
        self.session.edit_source('待润色原文。');self.session.set_polish_scope('source');self.session.polish('精简')
        self.assertEqual(self.api.calls[-1]['text'],'待润色原文。');self.assertEqual(self.api.calls[-1]['scope'],'source')
        replaced=[];self.session.source_replaced.connect(replaced.append);self.api.finish('精简的原文。')
        self.assertEqual(self.session.source_text,'精简的原文。');self.assertEqual(self.session.result,'');self.assertEqual(replaced,['精简的原文。'])
    def test_polish_instruction_locks_the_input_language(self):
        self.translated();self.session.polish('更亲切')
        self.assertIn('保持输入文本的语言',self.api.calls[-1]['instruction'])
    def test_polish_rejects_a_result_in_a_different_script(self):
        self.translated();self.session.polish('更亲切');self.api.finish('A completely different language.')
        self.assertEqual(self.session.result,'基准译文。')
        self.assertEqual(self.session.phase,'error')
        self.assertIn('语言',self.session.error)
    def test_polish_accepts_a_result_in_the_same_script(self):
        self.translated();self.session.polish('更亲切');self.api.finish('更亲切的译文。')
        self.assertEqual(self.session.result,'更亲切的译文。')
        self.assertFalse(self.session.error)
    def test_source_polish_rejects_a_different_language(self):
        self.session.edit_source('待润色原文。');self.session.set_polish_scope('source');self.session.polish('精简')
        self.api.finish('Rewritten source.')
        self.assertEqual(self.session.source_text,'待润色原文。')
        self.assertEqual(self.session.phase,'error')
    def test_switching_polish_scope_cancels_old_source_completion(self):
        self.session.edit_source('待润色原文。');self.session.set_polish_scope('source');self.session.polish('精简');epoch=self.api.calls[-1]['epoch']
        self.session.set_polish_scope('target');self.api.completed.emit(epoch,'过期的原文。','')
        self.assertEqual(self.session.source_text,'待润色原文。');self.assertEqual(self.session.polish_scope,'target')
    def test_history_restore_never_calls_provider(self):
        self.translated();self.session.remember();record=deepcopy(self.store.data['history'][0]);count=len(self.api.calls)
        self.session.restore_record(record);settle(timeout=.04);self.assertEqual(len(self.api.calls),count)
    def test_delete_current_record_does_not_reappear_on_shutdown(self):
        self.translated();self.session.remember();self.store.data['history'].clear();self.session.forget_record();self.session.close()
        self.assertFalse(self.store.data['history'])
    def test_delay_is_seconds_with_bounded_slider_range(self):
        self.session.set_delay(1700);self.assertEqual(self.session.delay_seconds,1.7)
        self.session.set_delay(90000);self.assertEqual(self.session.delay_seconds,5)
        self.session.set_delay(0);self.assertEqual(self.session.delay_seconds,.1)
    def test_swap_requires_known_source_and_preserves_expected_content(self):
        self.translated();self.session.detected_code='en';self.session.swap()
        self.assertEqual((self.session.source,self.session.target),('en','en'))
        self.assertEqual(self.session.source_text,'基准译文。')
    def test_no_request_for_oversized_input(self):
        self.session.edit_source('x'*16001);self.assertFalse(self.session.translate());self.assertFalse(self.api.calls);self.assertTrue(self.session.error)


class StorageTests(unittest.TestCase):
    def test_no_classification_tags_and_validated_delay(self):
        data=defaults();data.update(source=[],target={},debounce_ms=1700)
        result=normalize(data);self.assertEqual(result['source'],'auto');self.assertEqual(result['target'],'en');self.assertEqual(result['debounce_ms'],1700)
        self.assertNotIn('tags',result)
    def test_async_roundtrip_and_corrupt_history_never_overwrites_original(self):
        with tempfile.TemporaryDirectory() as directory:
            store=TranslatorStore(directory,autoload=False);store.update_preferences(debounce_ms=2400);store.close()
            store=TranslatorStore(directory);self.assertTrue(settle(lambda:store.ready,2));self.assertEqual(store.data['debounce_ms'],2400);store.close()
            path=Path(directory)/'translator.json';raw=b'{damaged history';path.write_bytes(raw)
            store=TranslatorStore(directory);self.assertTrue(settle(lambda:store.ready,2));self.assertFalse(store.writable)
            store.update_preferences(debounce_ms=1000);store.close();self.assertEqual(path.read_bytes(),raw)
    def test_history_bounded_by_records_and_characters(self):
        document=defaults();document['history']=[dict(id=str(index),source_text='s'*16000,text='t'*24000,original='o'*24000,
            source='en',target='zh',created_at='2026-11-17T01:00:00+00:00') for index in range(400)]
        rows=normalize(document)['history'];self.assertLessEqual(len(rows),MAX_HISTORY)
        self.assertLessEqual(sum(len(row[key]) for row in rows for key in ('source_text','text','original')),MAX_HISTORY_CHARS)


class ApiTests(unittest.TestCase):
    class Cloud:
        def __init__(self):self.calls=[]
        def request(self,method,path,callback,**kwargs):self.calls.append((callback,kwargs));return len(self.calls)
        def cancel(self,request):pass
    def test_legacy_payload_is_unchanged(self):
        cloud=self.Cloud();api=TranslationApi(cloud);api.translate(1,'hello','zh')
        self.assertEqual(set(cloud.calls[0][1]['data']),{'text','target','request_id'})
    def test_old_server_cannot_fake_a_successful_polish(self):
        cloud=self.Cloud();api=TranslationApi(cloud);out=[];api.completed.connect(lambda *value:out.append(value))
        api.polish(3,'译文','zh','精简');cloud.calls[-1][0]({'translation':'无操作标记的结果'},None)
        self.assertTrue(out[-1][2]);self.assertEqual(out[-1][1],'')
    def test_advanced_reply_and_stale_completion(self):
        cloud=self.Cloud();api=TranslationApi(cloud);out=[];api.completed.connect(lambda *value:out.append(value))
        api.polish(3,'译文','zh','精简');previous=cloud.calls[-1][0];api.polish(4,'译文','zh','商务')
        previous({'translation':'旧','operation':'polish'},None);self.assertFalse(out)
        cloud.calls[-1][0]({'translation':'新','operation':'polish'},None);self.assertEqual(out[-1],(4,'新',''))
    def test_source_polish_requires_explicit_scope_echo(self):
        cloud=self.Cloud();api=TranslationApi(cloud);out=[];api.completed.connect(lambda *value:out.append(value))
        api.polish(1,'原文','en','精简',scope='source');self.assertEqual(cloud.calls[-1][1]['data']['polish_scope'],'source')
        cloud.calls[-1][0]({'translation':'Wrong language','operation':'polish'},None);self.assertTrue(out[-1][2])
        api.polish(2,'原文','en','精简',scope='source');cloud.calls[-1][0]({'translation':'精简原文','operation':'polish','polish_scope':'source'},None)
        self.assertEqual(out[-1],(2,'精简原文',''))


class NativePanelTests(unittest.TestCase):
    def test_minimum_size_keeps_complete_character_counts_and_actions(self):
        from app_settings import SettingsStore
        from native_ui_common import configure_reference_font
        from translator_panel import TranslatorDialog
        from ui_transitions import WindowFade
        configure_reference_font(APP)
        with tempfile.TemporaryDirectory() as directory,patch.object(WindowFade,'enabled',return_value=False):
            settings=SettingsStore(directory);shelf=QWidget();store=TranslatorStore(directory,autoload=False)
            view=TranslatorDialog(SimpleNamespace(settings=settings,shelf=shelf),api=FakeApi(),store=store,detector=lambda text:'英语')
            try:
                view.show();view.resize(800,570);view.session.source_text='s'*16000;view.session.result='t'*24000
                for live in (True,False):
                    store.data['live']=live;view.refresh();settle(timeout=.03)
                    self.assertGreaterEqual(view.counter.width(),view.counter.fontMetrics().horizontalAdvance('16,000 字符'))
                    self.assertGreaterEqual(view.status.width(),view.status.fontMetrics().horizontalAdvance('24,000 字符'))
                    self.assertLessEqual(view.copy_button.mapTo(view.output_card,QPoint(view.copy_button.width(),0)).x(),view.output_card.width())
            finally:view.shutdown();view.deleteLater();shelf.deleteLater();settle(timeout=.02)
    def test_native_panels_keep_real_controls_and_no_settings_or_shortcuts(self):
        from app_settings import SettingsStore
        from translator_panel import TranslatorDialog
        from ui_transitions import WindowFade
        import ui_theme
        with tempfile.TemporaryDirectory() as directory,patch.object(WindowFade,'enabled',return_value=False),patch('PySide6.QtGui.QScreen.availableGeometry',return_value=QRect(0,0,1800,1200)):
            settings=SettingsStore(directory);shelf=QWidget();manager=SimpleNamespace(settings=settings,shelf=shelf)
            store=TranslatorStore(directory,autoload=False);api=FakeApi();view=TranslatorDialog(manager,api=api,store=store,detector=lambda text:'英语')
            try:
                view.show();settle();self.assertFalse(hasattr(view,'settings_button'))
                self.assertFalse(hasattr(view,'theme_button'));view._position_swap()
                self.assertEqual(view.source_language.mapTo(view.workspace,QPoint()).x(),view.source_card.x())
                self.assertEqual(view.target_language.mapTo(view.workspace,QPoint()).x(),view.output_card.x())
                self.assertLessEqual(abs(view.swap_button.geometry().center().x()-(view.source_card.geometry().right()+view.output_card.x())/2),1)
                self.assertEqual(view.polish_scope_segment.parentWidget(),view.composer_surface);self.assertTrue(view.polish_scope_buttons['target'].isChecked())
                for control in (view.glossary_button,view.presets_button,view.history_button,view.copy_button):self.assertEqual(control.property('referenceVariant'),'secondary')
                self.assertFalse(view.findChildren(QShortcut))
                self.assertEqual(view.delay_slider.minimum(),1);self.assertEqual(view.delay_slider.maximum(),50)
                view.delay_slider.setValue(17);self.assertEqual(view.session.delay_seconds,1.7)
                view.store.data['live']=False;view.source_editor.setPlainText('Synthetic input');view.session.translate();api.finish('示例译文。');settle()
                self.assertEqual(view.output_editor.toPlainText(),'示例译文。')
                self.assertTrue(view.status.text().startswith('5 字符'))
                view.instruction.setText('亲切');view.polish_button.click();api.finish('亲切的示例译文。');settle()
                self.assertTrue(view.undo_button.isVisible());view.session.undo();self.assertEqual(view.output_editor.toPlainText(),'示例译文。')
                for theme in ('dark','light'):
                    settings.update(theme=theme);ui_theme.set_theme(theme);view.refresh();settle(timeout=.03)
                    self.assertEqual(view._theme,theme)
                view.session.remember();view.toggle_history();settle();self.assertTrue(view.history.opened)
                view.toggle_history();settle();self.assertFalse(view.history.opened)
                view.polish_scope_buttons['source'].click();view.instruction.setText('精简');view.polish_button.click();api.finish('Rewritten source.');settle()
                self.assertEqual(view.source_editor.toPlainText(),'Rewritten source.');self.assertTrue(view.source_editor.document().isUndoAvailable())
                view.source_editor.undo();settle();self.assertEqual(view.session.source_text,'Synthetic input')
                view.show_language('target');settle();self.assertTrue(view.language_popups['target'].isVisible())
                view.hide();self.assertFalse(view.language_popups['target'].isVisible());self.assertFalse(view.session.debounce.isActive())
            finally:view.shutdown();view.close();view.deleteLater();shelf.deleteLater();settle()


if __name__=='__main__':unittest.main(verbosity=2)

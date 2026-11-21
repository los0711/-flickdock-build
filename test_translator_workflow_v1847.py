"""Regression tests for draft approval, explicit actions and precise alignment."""
import tempfile
import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from test_translator_v1843 import FakeApi, settle
import test_translator_v1844 as fixtures
from translation_alignment import SelectionAlignment
from translation_document import TranslationDocument, split_units
from translator_session import TranslatorSession
from translator_store import TranslatorStore


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=TranslatorStore(self.directory.name,autoload=False)
        self.store.data.update(live=False,debounce_ms=100,source='en',target='zh')
        self.api=FakeApi();self.state=TranslatorSession(self.api,self.store)
        self.addCleanup(self.state.close)
        self.state.edit_source('Initial source.')
        self.state.translate();self.api.finish('原有译文。')

    def draft(self):
        self.state.polish('Refine this',scope='source')
        self.api.finish('Polished source.')
        self.assertTrue(self.state.source_unconfirmed)
        self.assertEqual(self.state.result,'原有译文。')
        self.assertFalse(self.state.debounce.isActive())
        self.assertFalse(self.state.translate())

    def test_confirmation_translates_once_even_in_manual_mode(self):
        self.draft()
        count=len(self.api.calls)
        self.state.replace_original('source')
        self.assertFalse(self.state.source_unconfirmed)
        self.assertEqual(len(self.api.calls),count+1)
        self.assertEqual(self.state.result,'原有译文。')
        self.api.finish('新的译文。')
        self.assertEqual(self.state.result,'新的译文。')

    def test_editing_draft_debounces_one_new_translation(self):
        self.draft()
        count=len(self.api.calls)
        self.state.edit_source('Polished source, edited')
        self.state.edit_source('Polished source, edited again.')
        self.assertFalse(self.state.source_unconfirmed)
        self.assertEqual(len(self.api.calls),count)
        self.assertTrue(settle(lambda:len(self.api.calls)==count+1,1))
        self.assertEqual(self.state.result,'原有译文。')
        self.api.finish('编辑后的译文。')
        self.assertEqual(self.state.result,'编辑后的译文。')

    def test_clearing_source_cancels_late_results_and_clears_both_panes(self):
        self.draft()
        self.state.replace_original('source')
        epoch=self.state.epoch
        self.state.edit_source('')
        self.api.completed.emit(epoch,'过期结果。','')
        self.assertEqual(self.state.result,'')
        self.assertFalse(self.state.source_unconfirmed)

    def test_restoring_draft_does_not_translate(self):
        self.store.data['live']=True
        self.draft()
        count=len(self.api.calls)
        self.state.restore_original('source')
        self.assertEqual(self.state.source_text,'Initial source.')
        self.assertFalse(self.state.source_unconfirmed)
        self.assertFalse(self.state.debounce.isActive())
        self.assertEqual(len(self.api.calls),count)


class AlignmentTests(unittest.TestCase):
    def test_disjoint_quotes_survive_moving_sentence_offsets(self):
        doc=TranslationDocument();api=fixtures.UnitsApi()
        alignment=SelectionAlignment(doc,api);self.addCleanup(alignment.cancel)
        out=[];alignment.highlight.connect(lambda *values:out.append(values))
        text='We will pick up the parcels.'
        target='Wir holen die Pakete ab.'
        units=split_units(text);units[0].translation=target
        doc.commit(text,units,('en','de',()))
        i=text.index('pick up')
        alignment.select('source',i,i+7,doc.source,doc.target);alignment.timer.stop();alignment._request()
        words=[dict(quote=word,start=target.index(word),end=target.index(word)+len(word)) for word in ('holen','ab')]
        api.aligned.emit(alignment.epoch,dict(certain=True,matches=words),'')
        self.assertEqual(out[-1][1],[(w['start'],w['end']) for w in words])
        source='Intro. '+text;units=split_units(source)
        units[0].translation='Hallo.';units[1].translation=target
        doc.commit(source,units,('en','de',()))
        j=source.index('pick up');offset=doc.target.index(target)
        alignment.select('source',j,j+7,doc.source,doc.target)
        self.assertEqual(out[-1][1],[(w['start']+offset,w['end']+offset) for w in words])


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.NativeFixTests()
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.view=self.fixture.view

    def test_enter_in_instruction_runs_polish_and_clear_action(self):
        self.view.instruction.setText('Use a warmer tone.')
        self.view.instruction.setFocus()
        calls=len(self.fixture.api.calls)
        QTest.keyClick(self.view.instruction,Qt.Key_Return)
        self.assertEqual(len(self.fixture.api.calls),calls+1)
        self.assertEqual(self.view.session.phase,'polishing')
        self.assertNotIn('glossary',self.view._managers)
        self.view.clear_instruction.click()
        self.assertFalse(self.view.instruction.text())

    def test_editing_protected_term_updates_selected_row(self):
        self.view.show_manager('glossary')
        editor=self.view._managers['glossary']
        editor.list.setCurrentRow(0)
        editor.name.setText('FlickDock Pro')
        editor.update_button.click()
        self.assertEqual(self.fixture.store.data['glossary'],['FlickDock Pro'])
        self.assertEqual(editor.list.currentItem().text(),'FlickDock Pro')
        editor.close()

    def test_voice_card_has_independent_restart_and_pause_resume(self):
        self.view._speak(True)
        self.fixture.voice.finish_voice();self.fixture.voice.finish_audio()
        self.assertTrue(settle(lambda:self.fixture.speech.player is not None,.5))
        play=self.view.speech_playback['target']
        self.assertTrue(play.isVisible())
        play.click();self.assertTrue(self.fixture.speech.paused)
        play.click();self.assertFalse(self.fixture.speech.paused)
        self.assertEqual(self.view.speech_bars['target'][-2].glyph,'refresh')

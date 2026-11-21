"""Offscreen regression coverage for independent voices and overview layout."""
import os
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPointF

import test_html_native_workspace as workspace
import test_translator_v1844 as translator
from border_reveal import install_border_reveal
from translation_languages import SPEECH_LANGUAGES, TARGETS
from translator_store import normalize


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace.WorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_empty_category_leaves_preview_in_place(self):
        view = self.fixture.overview()
        view.select_item(self.fixture.paths[0])
        workspace.flush()
        preview = view.preview.geometry()
        view.set_category('audio')
        workspace.flush()
        self.assertTrue(view.empty_state.isVisible())
        self.assertTrue(view.preview.empty_preview.isVisible())
        self.assertIsNone(view.preview.current_path)
        self.assertEqual(view.preview.geometry(), preview)

    def test_scrollbar_appears_only_on_overflow_without_moving_card_edges(self):
        view = self.fixture.overview()
        for layout in ('list', 'icons'):
            view.change_view_mode(layout)
            workspace.until(lambda: len(view._item_widgets) == 3)
            self.assertFalse(view.scroll.verticalScrollBar().isVisible())
        ref = self.fixture.paths[0]
        width = view._item_widgets[ref].width()
        paths = []
        for index in range(18):
            path = self.fixture.base / f'additional-{index}.dat'
            path.write_bytes(b'fixture')
            paths.append(str(path))
        self.fixture.shelf.add_paths('permanent', paths)
        workspace.until(lambda: view.scroll.verticalScrollBar().isVisible() and len(view._item_widgets) == 21)
        self.assertGreater(view.scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(view._item_widgets[ref].width(), width)
        view.set_category('text')
        workspace.until(lambda: not view.scroll.verticalScrollBar().isVisible())

    def test_pointer_reveal_changes_cached_card_rim_only(self):
        tile = self.fixture.shelf.zones['permanent'].icon
        reveal = install_border_reveal(workspace.APP)
        reveal.set_position(None, None)
        self.addCleanup(reveal.set_position, None, None)
        ref, transform, _, front = tile.presentation_cards()[-1]
        self.assertTrue(front)
        edge = transform.map(QPointF(191.5, 64)).toPoint()
        center = transform.map(QPointF(96, 64)).toPoint()
        before = tile.grab().toImage()
        reveal.set_position(tile.mapToGlobal(edge), tile.window())
        workspace.flush()
        after = tile.grab().toImage()
        ratio = before.devicePixelRatio()
        x, y = round(edge.x()*ratio), round(edge.y()*ratio)
        self.assertTrue(any(before.pixelColor(i,y) != after.pixelColor(i,y) for i in range(x-2,x+2)))
        x, y = round(center.x()*ratio), round(center.y()*ratio)
        self.assertEqual(before.pixelColor(x,y), after.pixelColor(x,y))


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = translator.NativeFixTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_voice_names_and_synthesis_are_scoped_to_pane_and_language(self):
        f = self.fixture
        view = f.view
        view.session.set_language('source', 'en')
        view.show_tts('source')
        token, code = f.voice.voices[-1]
        self.assertEqual(code, 'en')
        f.voice.voices_ready.emit(token, dict(voices=[
            dict(voice_id='source_voice', name='Source voice'),
            dict(voice_id='target_voice', name='Target voice'),
        ]), '')
        view._choose_voice('source_voice')
        self.assertEqual(view.tts_voice_button.text(), 'Source voice')
        view.show_tts('target')
        view._choose_voice('target_voice')
        self.assertEqual(view.tts_voice_button.text(), 'Target voice')
        view.show_tts('source')
        self.assertEqual(view.tts_voice_button.text(), 'Source voice')
        view._speak(False, force=True)
        self.assertEqual(f.voice.calls[-1]['voice'], 'source_voice')
        view.session.result = 'A target sentence.'
        view._speak(True, force=True)
        self.assertEqual(f.voice.calls[-1]['voice'], 'target_voice')
        view.show_tts('target')
        view.session.set_language('target', 'bg')
        self.assertFalse(view.tts_voice_button.isEnabled())
        f.voice.finish_voice()
        self.assertEqual([row['key'] for row in f.speech.catalog], ['voc_bg'])
        self.assertEqual(view.tts_voice_button.text(), '自动选择音色')
        view.session.set_language('target', 'en')
        self.assertEqual(view.tts_voice_button.text(), 'Target voice')

    def test_late_catalog_cannot_replace_current_language(self):
        f = self.fixture
        f.view.show_tts('target')
        old = f.voice.voices[-1][0]
        f.view.session.set_language('target', 'bn')
        f.voice.finish_voice()
        f.voice.voices_ready.emit(old, dict(voices=[dict(voice_id='old', name='Old')]), '')
        self.assertEqual([row['key'] for row in f.speech.catalog], ['voc_bn'])

    def test_legacy_voice_moves_to_target_and_new_selections_round_trip(self):
        data = normalize(dict(schema_version=1, target='en', tts_voice='legacy'))
        self.assertEqual(data['tts_voices'], dict(source={}, target={'en': dict(id='legacy', name='')}))
        data['tts_voices']['source']['en'] = dict(id='new', name='New voice')
        self.assertEqual(normalize(data)['tts_voices'], data['tts_voices'])


class LanguageTests(unittest.TestCase):
    def test_desktop_and_worker_language_catalogs_agree(self):
        import json
        import re
        root = Path(__file__).resolve().parent / 'cloud' / 'src'
        units = (root / 'translation_units.ts').read_text(encoding='utf8')
        names = json.loads(re.search(r'TRANSLATION_LANGUAGES: .*? = (\{.*?\});', units, re.S)[1])
        self.assertEqual(names, {code: label for code, (label, _) in TARGETS.items()})
        speech = (root / 'speech.ts').read_text(encoding='utf8')
        provider_codes = set(re.search(r"const LANGUAGES=new Set\('([^']+)'", speech)[1].split())
        self.assertEqual(SPEECH_LANGUAGES, provider_codes-{'no', 'tl'} | {'nb', 'fil', 'zh-Hant'})
        self.assertTrue(SPEECH_LANGUAGES <= TARGETS.keys())

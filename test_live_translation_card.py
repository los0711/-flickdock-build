"""Synthetic editor regression coverage for the floating, incremental translator."""
import tempfile
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QEnterEvent
from PySide6.QtWidgets import QApplication, QWidget

from live_translation import FOOTER_HEIGHT, LiveEditorTranslation, LiveTranslationCard, fit_card
from live_translation_draft import LiveDraft
from live_translation_editor import EditorSelection, EditorState, _application_scope, _is_web_document, _windows_web_context
from live_translation_motion import LiveTranslationMark, LiveTranslationNotice
from native_ui_common import colors
from translator_store import TranslatorStore
import ui_theme


def complete(request, *texts):
    _, data, finish = request
    rows = data['units']
    assert len(rows) == len(texts)
    result = [dict(id=row['id'], text=text) for row, text in zip(rows, texts)]
    finish(dict(operation='translate_units', units=result), None)


class UnitsApi(QObject):
    units_ready = Signal(int, object, str)

    def __init__(self):
        super().__init__()
        self.calls = []

    def cancel(self):
        return None

    def translate_units(self, generation, rows, target):
        self.calls.append((generation, rows, target))

    def resolve(self, *translations):
        generation, rows, _ = self.calls[-1]
        assert len(rows) == len(translations)
        translated = [dict(id=row['id'], text=text) for row, text in zip(rows, translations)]
        self.units_ready.emit(generation, {'units': translated}, '')


class Transport:
    def __init__(self):
        self.requests = []

    def request(self, method, path, finished, *, data, **kwargs):
        ticket = object()
        self.requests.append((ticket, data, finished))
        return ticket

    def cancel(self, ticket):
        return None


def wait_for(app, predicate, limit=1.5):
    deadline = time.monotonic() + limit
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.004)
    return predicate()


class LiveTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_global_translation_is_on_by_default_but_remembers_turning_it_off(self):
        from app_settings import SettingsStore
        with tempfile.TemporaryDirectory() as directory:
            settings = SettingsStore(directory)
            self.assertTrue(settings.live_editor_translation_enabled)
            settings.update(live_editor_translation_enabled=False)
            self.assertFalse(SettingsStore(directory).live_editor_translation_enabled)

    def test_document_apps_are_excluded_without_a_chat_allowlist(self):
        for platform, identifier in (
            ('win32', r'C:\Office\WINWORD.EXE'), ('win32', 'wps.exe'),
            ('darwin', 'com.apple.iWork.Pages'), ('darwin', 'com.apple.TextEdit'),
        ):
            self.assertEqual(_application_scope(platform, identifier), 'document')
        for platform, identifier in (
            ('win32', 'qq.exe'), ('win32', 'UnknownMessenger.exe'),
            ('darwin', 'org.telegram.desktop'), ('darwin', 'org.example.custom-editor'),
        ):
            self.assertEqual(_application_scope(platform, identifier), 'editor')
        self.assertEqual(_application_scope('win32', 'firefox.exe'), 'browser')
        self.assertEqual(_application_scope('darwin', 'com.apple.Safari'), 'browser')

    def test_only_document_sites_and_editor_routes_are_excluded(self):
        for address in (
            'https://docs.google.com/document/d/sample/edit',
            'https://web.kdocs.cn/l/sample', 'https://docs.qq.com/doc/sample',
            'https://www.notion.so/workspace/page', 'https://word.cloud.microsoft/',
            'https://www.office.com/launch/word', 'https://office.sharepoint.com/_layouts/15/Doc.aspx',
            'https://word-edit.officeapps.live.com/we/wordeditorframe.aspx',
        ):
            self.assertTrue(_is_web_document(address), address)
        for address in (
            None, 'https://web.whatsapp.com/', 'https://chatgpt.com/', 'https://claude.ai/chat/',
            'https://copilot.microsoft.com/', 'https://www.office.com/chat',
            'https://office.sharepoint.com/sites/home', 'https://docs.google.com.example.org/',
        ):
            self.assertFalse(_is_web_document(address), address)

    def test_windows_finds_document_ancestry_without_requiring_a_chat_label_or_url(self):
        class Automation:
            def __init__(self, address):
                self.address = address

            def object(self, provider, slot, *arguments):
                return 'walker' if slot == 16 else {'editor':'inner', 'inner':'tab'}.get(arguments[0])

            def read(self, provider, slot, value_type):
                return SimpleNamespace(value=50030 if provider == 'tab' else 50004)

            def pattern(self, provider, pattern, iid):
                return provider

            def string(self, provider, slot):
                return self.address

        for address, is_document in (('https://docs.google.com/document/d/test/edit', True),
                                     ('https://claude.ai/new', False), (None, False)):
            self.assertEqual(_windows_web_context(Automation(address), 'automation', 'editor', Event()),
                             (True,is_document))

    def test_append_sends_only_the_new_sentence(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        draft.edit('你好。预算已确认。', immediate=True)
        api.resolve('Hello.', 'The budget is approved.')
        draft.edit('你好。预算已确认。周五出货。', immediate=True)
        self.assertEqual(api.calls[-1][1], [{'id': '2', 'text': '周五出货。', 'before': '', 'after': ''}])
        api.resolve('We ship on Friday.')
        self.assertEqual(draft.result, 'Hello. The budget is approved. We ship on Friday.')

    def test_long_draft_is_translated_incrementally(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        original = '会议纪要的详细内容。' * 215
        self.assertGreater(len(original), 2000)
        draft.edit(original, immediate=True)
        for _ in range(2):
            rows = api.calls[-1][1]
            api.resolve(*('Meeting notes.' for _ in rows))
        self.assertTrue(draft.ready)
        draft.edit(original + '请回复。', immediate=True)
        self.assertEqual(api.calls[-1][1], [{'id': '215', 'text': '请回复。', 'before': '', 'after': ''}])
        api.resolve('Please reply.')
        self.assertTrue(draft.ready)

    def test_editing_the_middle_reuses_both_neighbors(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        draft.edit('你好。预算已确认。周五出货。', immediate=True)
        api.resolve('Hello.', 'The budget is approved.', 'We ship on Friday.')
        draft.edit('你好。预算还在审批。周五出货。', immediate=True)
        self.assertEqual(api.calls[-1][1], [
            {'id': '1', 'text': '预算还在审批。', 'before': '你好。', 'after': '周五出货。'}])
        api.resolve('The budget is pending.')
        self.assertEqual(draft.result, 'Hello. The budget is pending. We ship on Friday.')

    def test_burst_is_coalesced_and_superseded_result_is_never_shown(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        for text in ('你', '你好', '你好。'):
            draft.edit(text)
        self.assertEqual(api.calls, [])
        self.assertTrue(wait_for(self.app, lambda: len(api.calls) == 1))
        self.assertEqual(api.calls[-1][1][0]['text'], '你好。')
        draft.edit('你好！', immediate=True)
        self.assertEqual(len(api.calls), 1)
        api.resolve('Stale text.')
        self.assertEqual(draft.result, '')
        self.assertEqual(len(api.calls), 2)
        api.resolve('Hello!')
        self.assertTrue(draft.ready)
        self.assertEqual(draft.result, 'Hello!')

    def test_clearing_immediately_invalidates_in_flight_preview(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        draft.edit('你好。', immediate=True)
        draft.edit('')
        api.resolve('Late greeting.')
        self.assertEqual(draft.result, '')
        self.assertFalse(draft.ready or draft.loading)

    def test_drag_never_intersects_the_editor(self):
        area = QRect(0, 0, 1160, 740)
        editor = QRect(260, 260, 660, 130)
        for x in range(0, 1160, 16):
            for y in range(0, 740, 16):
                rect = fit_card(area, editor, QSize(400, 158), QPoint(x, y))
                self.assertTrue(area.contains(rect))
                self.assertFalse(rect.intersects(editor.adjusted(-9, -9, 9, 9)))
        self.assertTrue(fit_card(area, area, QSize(400, 158)).isEmpty())

    def test_language_tool_samples_one_finished_selection(self):
        bounds = (60, 150, 570, 110)
        field = [EditorState(('scenario',), bounds, '')]
        transport = Transport()
        samples = []
        selected = EditorSelection(('other-control',), 'Bonjour à tous')

        buttons = [0]

        def probe(cancel, *, sample=False, at_pointer=False):
            if sample:
                samples.append(at_pointer)
                return selected
            return field[0]

        with tempfile.TemporaryDirectory() as directory:
            store = TranslatorStore(directory, autoload=False)
            live = LiveEditorTranslation(transport, store, probe=probe)
            try:
                with patch('live_translation.content_language', return_value='fr'), \
                        patch('live_translation.mouse_buttons_mask', side_effect=lambda: buttons[0]):
                    live.start('en')
                    self.assertTrue(wait_for(self.app, lambda: live.editor is not None))
                    field[0] = EditorState(('scenario',), bounds, '今天有消息。')
                    self.assertTrue(wait_for(self.app, lambda: len(transport.requests) == 1))
                    complete(transport.requests[-1], 'There is news today.')
                    self.assertTrue(live.card.isVisible())
                    live.card.sample.setChecked(True)
                    live._toggle_sample()
                    self.assertEqual(samples, [])
                    buttons[0] = 1
                    self.assertTrue(wait_for(self.app, lambda: live._sample_pressed))
                    buttons[0] = 0
                    self.assertTrue(wait_for(self.app, lambda: live.draft.target == 'fr'))
                    self.assertFalse(live.card.sample.isChecked())
                    self.assertEqual(samples, [True])
                    self.assertTrue(live.card.isVisible())
                    self.assertEqual(live.card.result.toPlainText(), 'There is news today.')
                    self.assertFalse(live.card.insert.isEnabled())
                    self.assertEqual(len(transport.requests), 2)
                    field[0] = None
                    live._language_focus_grace = time.monotonic() - .05
                    self.assertTrue(wait_for(self.app, lambda: live._language_focus_grace > time.monotonic()))
                    self.assertTrue(live.card.isVisible())
                    complete(transport.requests[-1], 'Il y a des nouvelles aujourd’hui.')
                    self.assertTrue(live.draft.ready)
                    self.assertTrue(live.card.isVisible())
                    self.assertEqual(live.card.result.toPlainText(), 'Il y a des nouvelles aujourd’hui.')
                    field[0] = EditorState(('scenario',), bounds, '')
                    self.assertTrue(wait_for(self.app, lambda: not live.card.isVisible()))
            finally:
                live.stop()
                live.pool.waitForDone(1000)
                live.card.close()
                store.close()

    def test_hover_reveals_footer_below_stationary_text_and_tracks_input(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TranslatorStore(directory, autoload=False)
            card = LiveTranslationCard(store)
            try:
                def screen(bounds):
                    return QRect(*bounds), self.app.primaryScreen()

                with patch('live_translation._editor_geometry', side_effect=screen), \
                        patch('live_translation.reduced_motion', return_value=False), \
                        patch('native_ui_common.reduced_motion', return_value=False):
                    card.result.setPlainText('One line.')
                    first = (34, 100, 360, 36)
                    self.assertTrue(card.set_bounds(first))
                    card.present()
                    self.assertTrue(wait_for(self.app, lambda: card.motion.values['y'] == 0))
                    card._set_footer(False)
                    self.assertTrue(wait_for(self.app, card.footer_clip.isHidden))
                    original_host = card.geometry()
                    original_text = card.result.geometry()
                    collapsed = card.surface.height()
                    self.assertEqual(card.width(), 620)
                    self.assertEqual(card.y(), QRect(*first).bottom() + 11)
                    self.app.sendEvent(card, QEnterEvent(QPointF(10, 10), QPointF(10, 10), QPointF(10, 10)))
                    self.assertTrue(wait_for(self.app, lambda: collapsed < card.surface.height() < collapsed + FOOTER_HEIGHT))
                    self.assertTrue(wait_for(self.app, lambda: card.surface.height() == collapsed + FOOTER_HEIGHT))
                    self.assertEqual(card.geometry(), original_host)
                    self.assertEqual(card.result.geometry(), original_text)
                    self.app.sendEvent(card, QEvent(QEvent.Leave))
                    self.assertTrue(wait_for(self.app, lambda: collapsed < card.surface.height() < collapsed + FOOTER_HEIGHT))
                    self.assertTrue(wait_for(self.app, card.footer_clip.isHidden))
                    second = (first[0] + 90, first[1] + 85, *first[2:])
                    self.assertTrue(card.set_bounds(second))
                    card.present()
                    self.assertEqual(card.pos(), original_host.topLeft() + QPoint(90, 85))
                    self.assertFalse(card.geometry().intersects(QRect(*second)))
            finally:
                card.close()
                store.close()

    def test_live_card_and_text_viewport_use_the_application_backdrop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TranslatorStore(directory, autoload=False)
            previous_theme = ui_theme.current_theme()
            card = LiveTranslationCard(store)
            try:
                for theme in ('light', 'dark'):
                    with self.subTest(theme=theme):
                        ui_theme.set_theme(theme)
                        card.refresh_theme()
                        palette = colors(theme)
                        self.assertEqual(card.surface.surface_style(),
                                         (palette['window_backdrop'], palette['glass_border'], 14))
                        self.assertEqual(card.result._viewport_fill, QColor(palette['window_backdrop']))
            finally:
                card.close()
                store.close()
                ui_theme.set_theme(previous_theme)

    def test_follow_caret_and_selection_in_long_translation(self):
        bounds = (60, 105, 450, 120)
        samples = [EditorState(('long',), bounds, '')]
        original_sentences = [f'第 {index} 段消息。' for index in range(24)]
        source = '\n'.join(original_sentences)
        translated = [f'Translated paragraph number {index}.' for index in range(24)]
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory:
            store = TranslatorStore(directory, autoload=False)
            live = LiveEditorTranslation(transport, store, probe=lambda cancel: samples[0])
            try:
                with patch('live_translation._editor_geometry', return_value=(QRect(*bounds), self.app.primaryScreen())), \
                        patch('live_translation.mouse_buttons_mask', return_value=0):
                    live.start('en')
                    self.assertTrue(wait_for(self.app, lambda: live.editor is not None))
                    samples[0] = EditorState(('long',), bounds, source)
                    self.assertTrue(wait_for(self.app, lambda: len(transport.requests) == 1))
                    complete(transport.requests[-1], *translated)
                    scroll = live.card.result.verticalScrollBar()
                    self.assertGreater(scroll.maximum(), 0)
                    self.assertEqual(scroll.value(), scroll.maximum())
                    samples[0] = EditorState(('long',), bounds, source, (0, 0))
                    self.assertTrue(wait_for(self.app, lambda: scroll.value() == 0))
                    begin = source.index(original_sentences[-1])
                    samples[0] = EditorState(('long',), bounds, source, (begin, len(source)))
                    self.assertTrue(wait_for(self.app, lambda: bool(live.card.result.extraSelections())))
                    self.assertGreater(scroll.value(), 0)
                    highlights = live.card.result.extraSelections()
                    self.assertEqual(highlights[0].cursor.selectedText(), translated[-1])
                    changed = source.replace(original_sentences[10], '第十段已更改。', 1)
                    samples[0] = EditorState(('long',), bounds, changed)
                    self.assertTrue(wait_for(self.app, lambda: len(transport.requests) == 2))
                    self.assertEqual(len(transport.requests[-1][1]['units']), 1)
                    complete(transport.requests[-1], 'Paragraph ten was updated.')
                    self.assertGreater(scroll.value(), 0)
                    self.assertLess(scroll.value(), scroll.maximum())
            finally:
                live.stop()
                live.pool.waitForDone(1000)
                live.card.close()
                store.close()

    def test_lottie_notice_is_one_shot_and_mark_loops_while_visible(self):
        parent = QWidget()
        parent.resize(600, 540)
        parent.show()
        notice = LiveTranslationNotice(parent)
        marker = LiveTranslationMark(parent)
        with patch('live_translation_motion.reduced_motion', return_value=False):
            notice.present()
            self.assertTrue(notice.isVisible())
            self.assertTrue(wait_for(self.app, lambda: not notice.isVisible(), limit=2.2))
            self.assertFalse(notice.pulse.isActive())
            marker.set_running(True)
            self.assertEqual(marker.repeat.interval(), 3000)
            self.assertTrue(marker.repeat.isActive())
            first = marker.started
            self.assertTrue(wait_for(self.app, lambda: marker.started is not None and marker.started != first, limit=3.4))
            marker.set_running(False)
            self.assertFalse(marker.repeat.isActive() or marker.pulse.isActive())
        parent.close()

    def test_popup_after_typing_clears_and_inserts_without_a_clipboard_call(self):
        bounds = (60, 150, 570, 110)
        fixture = [EditorState(('test-editor',), bounds, 'Existing text')]
        inserted = []

        def probe(cancel, *, expected=None, insertion=None):
            if cancel.is_set():
                return None
            if insertion is not None:
                if expected != fixture[0]:
                    return False
                inserted.append(insertion)
                fixture[0] = EditorState(('test-editor',), bounds, insertion)
                return True
            return fixture[0]

        with tempfile.TemporaryDirectory() as directory:
            store = TranslatorStore(directory, autoload=False)
            transport = Transport()
            live = LiveEditorTranslation(transport, store, probe=probe)
            try:
                with patch('live_translation._editor_geometry', return_value=(QRect(*bounds), self.app.primaryScreen())):
                    live.start('en')
                    self.assertTrue(wait_for(self.app, lambda: live.editor is not None))
                    self.assertFalse(live.card.isVisible())
                    self.assertEqual(transport.requests, [])
                    fixture[0] = EditorState(('test-editor',), bounds, '这是新消息。')
                    self.assertTrue(wait_for(self.app, live.card.isVisible))
                    self.assertTrue(wait_for(self.app, lambda: bool(transport.requests)))
                    _, data, finish = transport.requests[-1]
                    self.assertEqual(data['units'][0]['text'], '这是新消息。')
                    self.assertTrue(live.card.geometry().intersected(QRect(*bounds)).isEmpty())
                    finish(dict(operation='translate_units', units=[dict(id='0', text='This is a new message.')]), None)
                    self.assertTrue(live.card.insert.isEnabled())
                    self.assertTrue(live.card.handle.isHidden())
                    self.assertFalse(live.card.result.property('scrollEdgeBlur'))
                    self.assertEqual(live.card.result.verticalScrollBar().maximum(), 0)
                    self.assertLessEqual(live.card.surface.height(), 64)
                    completed_editor = fixture[0]
                    fixture[0] = None
                    self.assertTrue(wait_for(self.app, lambda: not live.card.isVisible()))
                    fixture[0] = completed_editor
                    self.assertTrue(wait_for(self.app, live.card.isVisible))
                    self.assertEqual(live.card.result.toPlainText(), 'This is a new message.')
                    self.assertEqual(len(transport.requests), 1)
                    live.card._set_footer(True)
                    self.assertTrue(wait_for(self.app, lambda: live.card.footer_clip.height() == FOOTER_HEIGHT))
                    live.card._choose_target()
                    self.assertTrue(live.card.popup.isVisible())
                    self.assertTrue(live.card.popup.windowFlags() & Qt.WindowStaysOnTopHint)
                    self.assertFalse(live.card.popup.geometry().intersects(QRect(*bounds)))
                    live.card.popup.hide()
                    live.card.insert.click()
                    self.assertTrue(wait_for(self.app, lambda: bool(inserted)))
                    self.assertEqual(inserted, ['This is a new message.'])
                    self.assertTrue(wait_for(self.app, lambda: not live.card.isVisible() and live.editor == fixture[0]))
                    fixture[0] = EditorState(('test-editor',), bounds, '再试一次。')
                    self.assertTrue(wait_for(self.app, live.card.isVisible))
                    fixture[0] = EditorState(('test-editor',), bounds, '')
                    self.assertTrue(wait_for(self.app, lambda: not live.card.isVisible()))
                    self.assertFalse(live.draft.ready)
            finally:
                live.stop()
                live.pool.waitForDone(1000)
                live.card.close()
                store.close()


if __name__ == '__main__':
    unittest.main()

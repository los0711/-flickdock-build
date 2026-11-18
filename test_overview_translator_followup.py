"""Regressions for the preview continuation, preset migration and settled shelf."""
import sys
from unittest import TestCase, skipUnless
from unittest.mock import patch

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QFontDatabase, QTextCursor, QTextLayout
from PySide6.QtTest import QTest

import test_html_native_workspace as workspace
import test_translator_v1844 as translator
from native_ui_common import configure_reference_font, technical_font
from test_translator_v1843 import settle
from translation_alignment import SelectionAlignment
from translation_document import TranslationDocument, split_units
from translator_store import DEFAULT_PRESETS, LEGACY_PRESETS, normalize


def rendered_families(text,font):
    layout=QTextLayout(text,font)
    layout.beginLayout()
    layout.createLine()
    layout.endLayout()
    return {run.rawFont().familyName() for run in layout.glyphRuns()}


class FontPolicyTests(TestCase):
    def test_mac_uses_platform_general_font_with_pingfang_fallback(self):
        app=workspace.APP
        self.addCleanup(app.setFont,app.font())
        with patch('native_ui_common.sys.platform','darwin'),patch.object(
                QFontDatabase,'systemFont',return_value=QFont('.AppleSystemUIFont')) as system_font:
            font=configure_reference_font(app)
            system_font.assert_called_once_with(QFontDatabase.SystemFont.GeneralFont)
            self.assertEqual(font.families(),['.AppleSystemUIFont','PingFang SC'])
            self.assertEqual(technical_font().families(),['JetBrains Mono',*font.families()])

    @skipUnless(sys.platform=='win32','requires installed Windows fonts')
    def test_windows_list_description_uses_yahei_and_timestamp_uses_mono(self):
        fixture=workspace.WorkspaceTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        view=fixture.overview()
        row=view._item_widgets[fixture.paths[0]]
        expected={'Microsoft YaHei'}
        self.assertEqual(rendered_families('FlickDock',view.title.font()),expected)
        self.assertEqual(rendered_families('report.txt',row.name_label.font()),expected)
        self.assertEqual(rendered_families('12 KB · TXT 文字',row.detail_label.font()),expected)
        self.assertTrue(row.detail_label._draw_timestamp)
        self.assertEqual(rendered_families('2026-11-18',row.detail_label._mono_font),{'JetBrains Mono'})


class OverviewMotionTests(TestCase):
    def test_snapshot_preparation_discards_a_queued_file_reflow(self):
        fixture=workspace.WorkspaceTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        view=fixture.overview();motion=view.item_transitions
        with patch('ui_transitions.motion_allowed',return_value=True):
            before={ref:widget.geometry() for ref,widget in view._item_widgets.items()}
            positions=motion.capture(view._item_widgets)
            positions={ref:pos+QPoint(0,40) for ref,pos in positions.items()}
            motion.reflow(view._item_widgets,positions,enabled=True)
            view._file_entry_requested=view._file_entry_pending=True
            view.prepare_transition();workspace.flush()
            self.assertFalse(motion.layout_pending or motion.entry_pending or motion.entry_started)
            self.assertFalse(view._file_entry_requested or view._file_entry_pending)
            self.assertFalse(motion.ghosts)
            self.assertEqual(before,{ref:widget.geometry() for ref,widget in view._item_widgets.items()})


class AlignmentTests(TestCase):
    def test_an_unchanged_sentence_still_highlights_while_the_new_input_is_waiting(self):
        old='First order. Confirm the delivery date.'
        document=TranslationDocument();units=split_units(old)
        units[0].translation='第一份订单。';units[1].translation='请确认交付日期。'
        result=document.commit(old,units,('en','zh',()))
        alignment=SelectionAlignment(document,translator.UnitsApi());self.addCleanup(alignment.cancel)
        visible=[];alignment.presentation.connect(lambda *args:visible.append(args))
        source='New intro. Confirm the delivery date.';start=source.index('delivery')
        alignment.select('source',start,start+len('delivery date'),source,result)
        span=(result.index('请确认'),len(result))
        self.assertEqual(visible[-1],('target',[],[span],'正在匹配划词'))
        alignment.timer.stop();alignment._request()
        alignment.api.aligned.emit(alignment.epoch,{'certain':False},'')
        self.assertEqual(visible[-1],('target',[],[span],'已定位到对应句子'))
        alignment.select('source',0,3,source,result)
        self.assertEqual(visible[-1],('target',[],[],'等待当前文本翻译后对齐'))


class TranslatorUITests(TestCase):
    def setUp(self):
        self.fixture=translator.NativeFixTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.view=self.fixture.view

    @skipUnless(sys.platform=='win32','requires installed Windows fonts')
    def test_windows_translation_text_and_controls_use_yahei_for_both_scripts(self):
        for widget in (self.view.source_editor,self.view.output_editor,self.view.copy_button):
            self.assertEqual(rendered_families('English 中文',widget.font()),{'Microsoft YaHei'})

    def test_target_comparison_closes_during_hover_and_can_be_reopened_and_restored(self):
        view=self.view;state=view.session
        baseline='\n'.join(f'译文第 {number} 行。' for number in range(30))
        view.source_editor.setPlainText('A longer source');state.translate();self.fixture.api.finish(baseline)
        self.assertTrue(state.polish('精简'));self.fixture.api.finish('精简译文。');settle(timeout=.03)
        with patch('translator_widgets.reduced_motion',return_value=True):view.original_view._expand(True)
        self.assertTrue(view.original_view._reveal.isVisible())
        close=view.close_target_comparison
        self.assertIs(view.childAt(close.mapTo(view,close.rect().center())),close)
        QTest.mouseClick(close,Qt.LeftButton)
        view.refresh()
        self.assertFalse(view.comparison.isVisible())
        self.assertFalse(view.original_view._reveal.isVisible())
        self.assertTrue(view.compare_button.isVisible())
        self.assertEqual(state.result,'精简译文。')
        view.compare_button.click()
        self.assertTrue(view.comparison.isVisible())
        view.restore_compare_button.click();self.assertEqual(state.result,baseline)
        self.assertFalse(view.comparison.isVisible())

    def test_source_change_retires_target_comparison_and_blocks_stale_restore(self):
        view=self.view;state=view.session
        self.assertTrue(state.polish('精简',scope='target'));self.fixture.api.finish('保留在右栏的译文。')
        self.assertTrue(view.comparison.isVisible())
        view._set_polish_scope('source')
        self.assertFalse(view.comparison.isVisible())
        self.assertTrue(state.polish('修改原文',scope='source'));self.fixture.api.finish('Unconfirmed draft.')
        self.assertTrue(view.source_comparison.isVisible())
        self.assertFalse(view.comparison.isVisible())
        state.restore_original('target');self.assertEqual(state.result,'保留在右栏的译文。')
        view.close_source_comparison.click();view.refresh()
        self.assertFalse(view.source_comparison.isVisible())
        self.assertTrue(state.source_unconfirmed)
        view.source_editor.setPlainText('Edited source.')
        view._set_polish_scope('target');state.restore_original('target')
        self.assertEqual(state.result,'保留在右栏的译文。')
        self.assertEqual(state.original,'')
        self.assertFalse(view.comparison.isVisible())
        self.assertFalse(view.original_view.toPlainText())

    def test_replacing_translation_reapplies_the_remaining_source_selection(self):
        view=self.view;source=view.source_editor
        c=source.textCursor();c.select(QTextCursor.Document);source.setTextCursor(c)
        first=view.output_editor.extraSelections()
        self.assertEqual(first[0].cursor.selectedText(),view.session.result)
        view.session.edit_result('新译文。')
        self.assertTrue(source.textCursor().hasSelection())
        second=view.output_editor.extraSelections()
        self.assertEqual(second[0].cursor.selectedText(),'新译文。')

    def test_selecting_after_a_windows_newline_and_emoji_keeps_offsets(self):
        view=self.view;state=view.session;source='😀 Intro.\r\nHello world.'
        state.open_input(source,'zh');units=split_units(source)
        units[0].translation='😀 引言。';units[1].translation='你好，世界。'
        state.result=state.document.commit(source,units,('en','zh',()));state.changed.emit()
        editor=view.source_editor;cursor=editor.textCursor()
        cursor.setPosition(len('😀 Intro.\n'.encode('utf-16-le'))//2)
        cursor.movePosition(QTextCursor.End,QTextCursor.KeepAnchor);editor.setTextCursor(cursor)
        self.assertEqual(cursor.selectedText(),'Hello world.')
        matches=view.output_editor.extraSelections()
        self.assertEqual(matches[0].cursor.selectedText(),'你好，世界。')

    def test_hover_reveals_the_following_lines_and_keeps_the_native_preview_geometry(self):
        view=self.view;document='\n'.join(f'Line {index:02d} with text to see after scrolling.' for index in range(40))
        view.source_editor.setPlainText(document);view._set_polish_scope('source')
        self.assertTrue(view.session.polish('更自然一些'));self.fixture.api.finish('Rewritten source text.');settle(timeout=.03)
        editor=view.source_baseline_view;bar=editor.verticalScrollBar()
        before=(editor.geometry(),editor.viewport().geometry(),bar.geometry(),bar.maximum())
        focus=QRect(editor.viewport().mapTo(view,QPoint()),editor.viewport().size())

        def region(image,area):
            scale=image.devicePixelRatio()
            return image.copy(QRect(*(round(value*scale) for value in area.getRect())))

        resting=region(view.grab().toImage(),focus)
        with patch('translator_widgets.reduced_motion',return_value=False):
            editor._expand(True)
            self.assertEqual(editor._reveal_animation.state(),editor._reveal_animation.Running)
            editor._reveal_animation.setCurrentTime(100)
            self.assertGreater(editor.reveal_progress,0);self.assertLess(editor.reveal_progress,1)
            editor._reveal_animation.setCurrentTime(210)
            self.assertTrue(editor._reveal.isVisible())
            self.assertTrue(editor._reveal.testAttribute(Qt.WA_TransparentForMouseEvents))
            reveal=editor._reveal.geometry()
            extent=focus.top()-reveal.top()
            self.assertGreater(extent,0)
            self.assertEqual(extent,reveal.bottom()-focus.bottom())
            comparison=editor.parentWidget()
            self.assertAlmostEqual(reveal.height(),comparison.height()*2.5,delta=1)
            pane=comparison.parentWidget()
            self.assertLess(reveal.top(),pane.mapTo(view,QPoint()).y())
            self.assertEqual(region(view.grab().toImage(),focus),resting)
            first=editor._reveal.grab().toImage()
            bar.setValue(8);settle(timeout=.03)
            scrolled=editor._reveal.grab().toImage()
            areas=(QRect(0,0,reveal.width(),extent),
                   QRect(0,extent+focus.height(),reveal.width(),extent))
            for area in areas:self.assertNotEqual(region(scrolled,area),region(first,area))
            self.assertEqual((editor.geometry(),editor.viewport().geometry(),bar.geometry(),bar.maximum()),before)
            editor._expand(False)
            editor._reveal_animation.setCurrentTime(85)
            self.assertTrue(editor._reveal.isVisible())
            editor._reveal_animation.setCurrentTime(175)
            self.assertFalse(editor._reveal.isVisible())
            self.assertEqual(bar.value(),8)


class PresetTests(TestCase):
    def test_all_builtin_presets_migrate_and_keep_the_current_language(self):
        rows=[dict(id=key,label=title,instruction=prompt) for key,title,prompt in LEGACY_PRESETS]
        custom=dict(id='custom',label='自定义',instruction='请翻译成英语')
        rows.append(custom)
        result=normalize(dict(schema_version=1,presets=rows))['presets']
        self.assertEqual([(row['id'],row['label'],row['instruction']) for row in result[:-1]],list(DEFAULT_PRESETS))
        self.assertEqual(result[-1],custom)
        self.assertTrue(all('当前文本的语言' in row['instruction'] for row in result[:-1]))
        rows[4]['instruction']='我自己的地道改写指令'
        self.assertEqual(normalize(dict(schema_version=1,presets=rows))['presets'][4],rows[4])

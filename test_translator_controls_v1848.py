"""Exercise the responsive window, its copy actions and the composed shelf edge."""
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QPointF, QEvent, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app_settings import SettingsStore
from native_ui_common import RADIUS_WINDOW_PX, colors
from reference_widgets import refresh_tree
import test_html_native_workspace as workspace
from test_translation import Account, Api, Clipboard
import test_translator_v1844 as translator
from test_translator_v1843 import settle
from translation_controller import TranslationController
from translation_alignment import SelectionAlignment
from translator_panel import COMPACT_FOOTER_WIDTH, TRANSLATED_INK
from translator_store import normalize
from translator_widgets import LanguageOptionDelegate
import ui_theme


class TranslatorControlsTests(unittest.TestCase):
    def setUp(self):
        fixture=translator.NativeFixTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        self.view=fixture.view;self.api=fixture.api
        theme=ui_theme.current_theme();self.addCleanup(ui_theme.set_theme,theme)
        ui_theme.set_theme(self.view.manager.settings.theme)

    def test_clear_undo_restores_the_previous_pair_without_api_calls(self):
        view=self.view;session=view.session
        pair=(session.source_text,session.result);document=session.document
        requests=len(self.api.calls)
        view.clear_source_button.click()
        self.assertEqual((session.source_text,session.result),('',''))
        self.assertEqual(view.clear_source_button.glyph,'undo')
        view.clear_source_button.click()
        self.assertEqual((session.source_text,session.result),pair)
        self.assertIs(session.document,document)
        self.assertEqual(len(self.api.calls),requests)
        view.clear_source_button.click();view.source_editor.setPlainText('New source text.')
        self.assertFalse(session.can_undo_clear)
        self.assertEqual(view.clear_source_button.glyph,'close')

    def test_detection_caption_holds_during_successive_edits(self):
        view=self.view;session=view.session
        self.assertTrue(settle(lambda:'英语' in view.source_language.text(),1.5))
        for text in ('你好','你好，世界'):
            view.source_editor.setPlainText(text)
            session.detected,session.detected_code='中文','zh';session.changed.emit()
            self.assertIn('英语',view.source_language.text())
            settle(timeout=.16)
            self.assertIn('英语',view.source_language.text())
        session.detection_timer.stop();session.detected,session.detected_code='中文','zh';session.changed.emit()
        self.assertTrue(settle(lambda:'中文' in view.source_language.text(),1.2))
        view.clear_source_button.click();self.assertEqual(view.source_language.text(),'自动检测')

    def test_language_stars_are_independent_and_list_rows_are_delegated(self):
        old=normalize({'schema_version':1,'favorites':['fr']})
        self.assertEqual(old['source_favorites'],['fr'])
        self.assertIsNot(old['source_favorites'],old['favorites'])
        view=self.view;view.show_language('source');popup=view.language_popups['source']
        popup.search.setText('法语');settle(timeout=.02)
        item=popup.list.item(0)
        self.assertIsNone(popup.list.itemWidget(item))
        code=item.data(Qt.UserRole);self.assertEqual(code,'fr')
        _,star=LanguageOptionDelegate.parts(popup.list.visualItemRect(item),code)
        target=view.store.favorites_for('target').copy()
        QTest.mouseClick(popup.list.viewport(),Qt.LeftButton,pos=star.center().toPoint())
        self.assertIn('fr',view.store.favorites_for('source'))
        self.assertEqual(view.store.favorites_for('target'),target)
        choice,_=LanguageOptionDelegate.parts(popup.list.visualItemRect(popup.list.item(0)),code)
        QTest.mouseClick(popup.list.viewport(),Qt.LeftButton,pos=choice.center().toPoint())
        self.assertEqual(view.session.source,'fr');self.assertFalse(popup.isVisible())

    def test_both_copy_buttons_silence_invitation_without_stopping_clipboard_delivery(self):
        view=self.view
        with tempfile.TemporaryDirectory() as directory, patch('intake_notice.motion_allowed',return_value=False):
            settings=SettingsStore(directory);settings.update(translation_enabled=True,translation_target='ja')
            clipboard=Clipboard();manager=SimpleNamespace(app=QApplication.instance(),settings=settings,clipboard_sync=clipboard,account=Account())
            controller=TranslationController(manager,api=Api());self.addCleanup(controller.deleteLater)
            view.manager.translation=controller
            received=[];clipboard.copy_observed.connect(received.append)
            def write(text):clipboard.copy_observed.emit(('text',{'text':text}))
            try:
                with patch.object(QApplication,'clipboard',return_value=SimpleNamespace(setText=write)),patch('platform_native.native_input_available',return_value=False):
                    view.source_copy_button.click();view.copy_button.click()
                self.assertEqual([payload[1]['text'] for payload in received],[view.session.source_text,view.session.result])
                self.assertIsNone(controller.notice)
                clipboard.copy_observed.emit(('text',{'text':'Copied in another application.'}))
                self.assertTrue(controller.notice.isVisible())
            finally:
                controller.close()
                if controller.notice:controller.notice.deleteLater()

    def test_resizing_keeps_both_columns_and_language_controls_aligned(self):
        view=self.view
        view.store.update_preferences(source_favorites=['zh','en','ja','fr'],favorites=['pt','ru','de','es'])
        for width in (1040,943,890,800,801,944,1040):
            view.resize(width,620);settle(timeout=.01)
            left,right=view.source_card,view.output_card
            self.assertLessEqual(abs(left.width()-right.width()),1)
            self.assertEqual(left.height(),right.height())
            self.assertEqual(left.x(),view.source_language.parentWidget().x())
            self.assertEqual(right.x(),view.target_language.parentWidget().x())
            gap_center=(left.x()+left.width()+right.x())/2
            self.assertLessEqual(abs(view.swap_button.x()+view.swap_button.width()/2-gap_center),1)
            compact=width<COMPACT_FOOTER_WIDTH
            for button in (view.source_copy_button,view.copy_button):
                self.assertEqual(ui_theme.allows_hover_hint(button),compact)
                if compact:self.assertEqual(button.width(),button.height())
            self.assertFalse(ui_theme.allows_hover_hint(view.history_button))
            self.assertTrue(ui_theme.allows_hover_hint(view.clear_source_button))

    def test_client_drag_preserves_window_shape_and_uses_its_final_position(self):
        view=self.view;view.resize(1040,640);settle(timeout=.03)
        chrome=view.chrome;chrome._system_resize=False
        start=view.surface.geometry().bottomRight();origin=view.mapToGlobal(start)
        def drag(kind,offset,button,buttons):
            event=QMouseEvent(kind,QPointF(start+offset),QPointF(origin+offset),button,buttons,Qt.NoModifier)
            return chrome.eventFilter(view,event)
        self.assertTrue(drag(QEvent.MouseButtonPress,QPoint(),Qt.LeftButton,Qt.LeftButton))
        for offset in (QPoint(-28,-12),QPoint(-110,-32),QPoint(-220,-48)):
            self.assertTrue(drag(QEvent.MouseMove,offset,Qt.NoButton,Qt.LeftButton))
            settle(timeout=.01)
            grab=view.grab().toImage();center=view.surface.geometry().center()*grab.devicePixelRatio()
            self.assertEqual(grab.pixelColor(center).alpha(),255)
        self.assertTrue(drag(QEvent.MouseButtonRelease,offset,Qt.LeftButton,Qt.NoButton))
        self.assertEqual(view.size().toTuple(),(820,592));self.assertIsNone(chrome._drag)

    def test_success_is_green_and_expiring_it_preserves_a_later_error(self):
        view=self.view;view._speech_exported('sample.mp3')
        self.assertIn('语音已保存',view.error_label.text())
        self.assertIn(colors(view.manager.settings.theme)['green'],view.error_label.styleSheet())
        view.session.error='测试错误';view._expire_notice()
        self.assertEqual(view.error_label.text(),'测试错误')
        self.assertIn(colors(view.manager.settings.theme)['red'],view.error_label.styleSheet())

    def test_typography_and_result_ink(self):
        view=self.view;view._refresh_presets();settle(timeout=.02)
        self.assertEqual(view.source_editor.font().pixelSize(),15)
        self.assertEqual(view.output_editor.font().pixelSize(),15)
        self.assertEqual(view.output_editor.palette().color(QPalette.Text),QColor(TRANSLATED_INK[view.manager.settings.theme]))
        self.assertEqual(view.preset_layout.itemAt(0).widget().font().pixelSize(),12)
        for control in (view.glossary_button,view.presets_button,view.history_button):
            self.assertEqual(control.font().pixelSize(),12)

    def test_search_focus_ring_and_selection_use_the_current_accent(self):
        import theme_accent
        view=self.view
        original=theme_accent.current()
        try:
            for theme in ('dark','light'):
                view.manager.settings.update(theme=theme)
                for accent in ('#000000','#0066D6','#9D36C8'):
                    theme_accent.set_color(accent);view.refresh()
                    palette=colors(theme)
                    self.assertEqual(palette['ring'],palette['action'])
                    self.assertIn('QLineEdit:focus{border-color:'+palette['action']+';}',view.styleSheet())
        finally:theme_accent.set_color(original)

    def test_pointer_release_starts_alignment_and_switching_panes_clears_the_old_selection(self):
        view=self.view;api=translator.UnitsApi()
        view.alignment.cancel();view.alignment.deleteLater()
        view.alignment=SelectionAlignment(view.session.document,api,view)
        view.alignment.highlight.connect(view._selection_highlight)
        def drag(editor,start,stop):
            cursor=editor.textCursor();cursor.setPosition(start);first=editor.cursorRect(cursor).center()
            cursor.setPosition(stop);last=editor.cursorRect(cursor).center()
            QTest.mousePress(editor.viewport(),Qt.LeftButton,pos=first)
            QTest.mouseMove(editor.viewport(),last)
            QTest.mouseRelease(editor.viewport(),Qt.LeftButton,pos=last)
        drag(view.source_editor,6,11)
        self.assertEqual(view.source_editor.textCursor().selectedText(),'world')
        self.assertTrue(settle(lambda:len(api.calls)==1,.09))
        api.aligned.emit(view.alignment.epoch,{'certain':True,'quote':'世界'},'')
        matches=view.output_editor.extraSelections()
        self.assertEqual(matches[0].cursor.selectedText(),'世界')
        c=colors(view.manager.settings.theme)
        self.assertEqual(matches[0].format.background().color(),QColor(c['action']))
        self.assertEqual(matches[0].format.foreground().color(),QColor(c['on_action']))
        view.manager.settings.update(theme='light' if view.manager.settings.theme=='dark' else 'dark')
        view.refresh()
        matches=view.output_editor.extraSelections()
        c=colors(view.manager.settings.theme)
        self.assertEqual(matches[0].format.background().color(),QColor(c['action']))
        self.assertEqual(matches[0].format.foreground().color(),QColor(c['on_action']))
        drag(view.output_editor,3,5)
        self.assertEqual(view.output_editor.textCursor().selectedText(),'世界')
        self.assertFalse(view.source_editor.textCursor().hasSelection())
        matches=view.source_editor.extraSelections()
        self.assertEqual(matches[0].cursor.selectedText(),'world')
        self.assertEqual(len(api.calls),1)


class ComposedOutlineTests(unittest.TestCase):
    def test_final_outline_reaches_all_four_rounded_corners_and_straight_edges(self):
        fixture=workspace.WorkspaceTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        overview=fixture.overview();overview.select_item(fixture.paths[0]);workspace.flush()
        shelf=fixture.shelf;card=shelf.card
        for theme in ('light','dark'):
            fixture.settings.update(theme=theme);ui_theme.set_theme(theme);refresh_tree(shelf);workspace.flush()
            drawn=shelf.grab().toImage();dpr=drawn.devicePixelRatio()
            bounds=card.geometry();x,y,w,h=(round(value*dpr) for value in bounds.getRect())
            with patch.object(type(card),'surface_outline',return_value=QColor(Qt.transparent)):
                plain=shelf.grab().toImage()
            radius=round(RADIUS_WINDOW_PX*dpr)
            for left,top in ((x,y),(x+w-radius,y),(x,y+h-radius),(x+w-radius,y+h-radius)):
                changed=sum(drawn.pixel(px,py)!=plain.pixel(px,py)
                            for px in range(left,left+radius) for py in range(top,top+radius))
                self.assertGreaterEqual(changed, radius, (theme,dpr,left,top))
            for x0,y0 in ((x+w//2,y),(x+w//2,y+h-1),(x,y+h//2),(x+w-1,y+h//2)):
                self.assertTrue(any(drawn.pixel(i,j)!=plain.pixel(i,j) for i in range(x0-2,x0+3) for j in range(y0-2,y0+3)))

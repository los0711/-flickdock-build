"""Local persistence and the docked clipboard history interaction."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QEnterEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from clipboard_history_panel import ClipboardHistoryPanel,FOLDED_HEIGHT
from clipboard_history_store import ClipboardHistoryStore
APP=QApplication.instance() or QApplication([])


def sample_until(predicate):
    until=time.monotonic()+4
    while not predicate():
        if time.monotonic()>until:raise AssertionError('Qt clipboard history timed out')
        QTest.qWait(12)


class LocalClipboardStoreTests(unittest.TestCase):
    def test_persistence_pin_dedup_and_clear_across_reopens(self):
        with TemporaryDirectory() as directory:
            store=ClipboardHistoryStore(directory)
            one=('text',{'text':'第一条 QT 历史记录','html':'<b>第一条 QT 历史记录</b>'})
            two=('text',{'text':'https://example.com/latest'})
            first=store.execute('remember',one)
            second=store.execute('remember',two)
            image=QImage(50,32,QImage.Format_ARGB32);image.fill(QColor('#5370a0'))
            picture=store.execute('remember',('image',{'image':image}))
            self.assertIsNotNone(picture)
            self.assertEqual(first,store.execute('remember',one))
            store.execute('pin',second)
            reopened=ClipboardHistoryStore(directory)
            rows=reopened.execute('list',12,0)
            self.assertEqual(len(rows),3)
            self.assertEqual((rows[0]['id'],rows[0]['kind']), (second,'link'))
            self.assertEqual(reopened.execute('payload',first),one)
            self.assertFalse(QImage.fromData(reopened.execute('payload',picture)[1]).isNull())
            reopened.execute('remove',first)
            self.assertIsNone(ClipboardHistoryStore(directory).execute('payload',first))
            reopened.execute('clear')
            self.assertEqual(ClipboardHistoryStore(directory).execute('list',12,0),[])

    def test_file_entry_keeps_reference_and_leaves_original_untouched(self):
        with TemporaryDirectory() as directory:
            source=Path(directory)/'original.txt'
            source.write_bytes(b'untouched')
            store=ClipboardHistoryStore(directory)
            key=store.execute('remember',('files',[str(source)]))
            self.assertEqual(store.execute('payload',key),('file',[str(source)]))
            store.execute('clear')
            self.assertEqual(source.read_bytes(),b'untouched')


class ClipboardHistoryPanelTests(unittest.TestCase):
    def setUp(self):
        for path in ('clipboard_history_panel.reduced_motion','native_ui_common.reduced_motion'):
            mocked=patch(path,return_value=False)
            mocked.start();self.addCleanup(mocked.stop)
        self.panel=ClipboardHistoryPanel()
        self.panel.resize(332,370)
        self.rows=[dict(id=str(index),title='fixture',excerpt='fixture',updated=time.time(),
                        kind='text',pinned=False) for index in range(1,4)]
        self.panel.set_entries(self.rows)
        self.panel.show()
        QTest.qWait(30)
        APP.sendEvent(self.panel,QEvent(QEvent.Leave))
        QTest.qWait(380)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        QApplication.processEvents()

    def click(self,part,key=''):
        self.panel.repaint()
        rect=next(rect for name,identity,rect in self.panel._hits if name==part and identity==key)
        QTest.mouseClick(self.panel,Qt.LeftButton,Qt.NoModifier,rect.center().toPoint())

    def test_hover_spreads_cards_and_thumbnail_shifts_following_rows(self):
        self.assertEqual(self.panel._surface_height(),FOLDED_HEIGHT)
        self.assertFalse(self.panel.expanded)
        at=QPointF(60,75)
        APP.sendEvent(self.panel,QEnterEvent(at,at,QPointF(self.panel.mapToGlobal(at.toPoint()))))
        sample_until(lambda:self.panel._stack_progress>.99)
        self.panel.repaint()
        before=next(rect.top() for part,key,rect in self.panel._hits if part=='card' and key=='3')
        self.click('thumb','2')
        sample_until(lambda:self.panel._preview_progress>.99)
        self.panel.repaint()
        after=next(rect.top() for part,key,rect in self.panel._hits if part=='card' and key=='3')
        self.assertGreater(after,before+80)
        self.assertEqual(self.panel.selected,'2')
        self.click('thumb','1')
        self.assertLess(self.panel._preview_progress,.5)
        sample_until(lambda:self.panel._preview_progress>.99)
        self.assertEqual(self.panel.selected,'1')
        APP.sendEvent(self.panel,QEvent(QEvent.Leave))
        sample_until(lambda:self.panel._stack_progress<.01)
        self.assertFalse(self.panel.selected)

    def test_pin_delete_and_clear_are_separate_from_paste(self):
        activated=[];pinned=[];removed=[];cleared=[]
        self.panel.activated.connect(activated.append);self.panel.pinned.connect(pinned.append)
        self.panel.removed.connect(removed.append);self.panel.clear_requested.connect(lambda:cleared.append(True))
        self.click('pin','1');self.click('remove','1')
        self.assertEqual(pinned,['1']);self.assertEqual(removed,['1']);self.assertFalse(activated)
        self.click('clear');self.assertFalse(cleared)
        self.click('clear');self.assertEqual(cleared,[True])

    def test_popup_keeps_gap_and_above_fallback_attached(self):
        anchor=QRect(160,590,332,300)
        self.panel.place(anchor,QRect(0,0,900,940))
        self.assertTrue(self.panel._above)
        point=QPoint(self.panel.geometry().center().x(),anchor.top()-12)
        self.assertTrue(self.panel.contains_point(point))


class ClipboardHistoryRuntimeTests(unittest.TestCase):
    def test_selected_record_leaves_current_clipboard_content_in_place(self):
        from clipboard_history_runtime import ClipboardHistory
        from types import SimpleNamespace
        from unittest.mock import Mock
        from PySide6.QtCore import QObject,Signal
        class NativeFixture(QObject):
            copy_observed=Signal(object)
            set_local_observation=Mock()
        shelf=SimpleNamespace(hide_animated=Mock())
        manager=SimpleNamespace(app=APP,clipboard_sync=NativeFixture(),shelf=shelf)
        with TemporaryDirectory() as directory,patch('clipboard_history_runtime.private_clipboard',return_value=False):
            runtime=ClipboardHistory(manager,directory)
            try:
                runtime.set_enabled(True)
                runtime.observe(('text',{'text':'历史粘贴测试'}))
                runtime.observe(('text',{'text':'private test','_history_private':True}))
                sample_until(lambda:len(runtime.entries)==1)
                runtime._create_panel()
                runtime.panel.resize(332,360);runtime.panel.show()
                APP.clipboard().setText('当前剪贴板测试')
                with patch('clipboard_history_runtime.front_target',return_value='target'), \
                     patch('clipboard_history_runtime.insert_text',return_value=True) as insert:
                    runtime._activate(runtime.entries[0]['id'])
                    sample_until(lambda:insert.called)
                    insert.assert_called_once_with('历史粘贴测试','target')
                self.assertEqual(APP.clipboard().text(),'当前剪贴板测试')
                runtime.set_enabled(False)
                self.assertFalse(runtime.panel.isVisible())
                self.assertEqual(runtime.store.execute('list',12,0)[0]['title'],'历史粘贴测试')
            finally:runtime.close()


if __name__=='__main__':unittest.main()

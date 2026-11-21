"""Local persistence and the docked clipboard history interaction."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QEnterEvent, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from clipboard_history_panel import ClipboardHistoryPanel,FOLDED_HEIGHT,HOVER_REBOUND_PX,PANEL_HEIGHT_LIMIT,SCREEN_EDGE_GAP
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
        sample_until(lambda:self.panel._stack_progress==1.)
        self.panel.repaint()
        before=next(rect.top() for part,key,rect in self.panel._hits if part=='card' and key=='3')
        self.click('thumb','2')
        sample_until(lambda:self.panel._preview_progress>.99)
        self.panel.repaint()
        after=next(rect.top() for part,key,rect in self.panel._hits if part=='card' and key=='3')
        added=self.panel._preview_extra(self.rows[1])
        self.assertGreater(added,25)
        self.assertLess(added,60)
        self.assertAlmostEqual(after-before,added,delta=1)
        self.assertEqual(self.panel.selected,'2')
        self.click('thumb','1')
        self.assertLess(self.panel._preview_progress,.5)
        sample_until(lambda:self.panel._preview_progress>.99)
        self.assertEqual(self.panel.selected,'1')
        APP.sendEvent(self.panel,QEvent(QEvent.Leave))
        sample_until(lambda:self.panel._stack_progress<.01)
        self.assertFalse(self.panel.selected)

    def test_list_fits_few_rows_and_leaves_screen_margin_at_the_scroll_limit(self):
        area=QRect(0,0,1200,1040)
        self.panel.place(QRect(730,80,332,312),area)
        self.panel.show_all()
        sample_until(lambda:self.panel._stack_progress>.99)
        self.assertLess(self.panel._surface_height(),300)
        self.assertEqual(self.panel.scroll,0.)
        self.panel.set_entries([dict(self.rows[0],id=str(index)) for index in range(25)])
        sample_until(lambda:self.panel._entry_progress==1 and self.panel._stack_progress==1.)
        self.assertEqual(self.panel._surface_height(),PANEL_HEIGHT_LIMIT)
        visible_bottom=self.panel.y()+self.panel._content_top()+self.panel._surface_height()
        self.assertLessEqual(visible_bottom,area.bottom()+1-SCREEN_EDGE_GAP)
        self.assertGreater(self.panel._body_height(),self.panel._surface_height())

    def test_history_shares_the_shelf_motion_without_resizing_its_host(self):
        from PySide6.QtCore import QRectF
        area=QRect(0,0,1200,1000)
        for anchor in (QRect(570,40,332,280),QRect(570,690,332,280)):
            with self.subTest(above=anchor.top()>500):
                self.panel.hide()
                with patch('clipboard_history_panel.order_companion_behind') as order:
                    self.assertTrue(self.panel.place(anchor,area,shelf_alpha=0.))
                    self.assertFalse(self.panel.isVisible())
                    geometry=self.panel.geometry()
                    front=object()
                    self.panel.follow_shelf(.06,False,front=front)
                    self.assertTrue(self.panel.isVisible())
                    order.assert_called_once_with(self.panel,front)
                opening=[]
                for alpha in (.06,.22,.43,.62,.82,1.):
                    self.panel.follow_shelf(alpha,False)
                    frame=self.panel._panel_frame.translated(float(geometry.x()),float(geometry.y()))
                    self.assertTrue(QRectF(geometry).contains(frame))
                    self.assertEqual(self.panel.geometry(),geometry)
                    self.assertGreater(self.panel._companion_opacity,0.)
                    self.assertLessEqual(self.panel._companion_opacity,alpha)
                    opening.append(frame.top())
                    if alpha<.62:self.assertTrue(frame.intersects(QRectF(anchor)))
                direction=1 if not self.panel._above else -1
                self.assertTrue(all((b-a)*direction>0 for a,b in zip(opening,opening[1:])))
                self.panel.show_all()
                self.panel._stack.stop()
                self.panel._stack_frame(1.)
                self.panel.follow_shelf(.29,True)
                frame=self.panel._panel_frame.translated(float(geometry.x()),float(geometry.y()))
                self.assertTrue(QRectF(geometry).contains(frame))
                self.assertTrue(frame.intersects(QRectF(anchor)))
                self.assertIsNone(self.panel._hit(QPointF(100,120)))
                self.panel.follow_shelf(0.,True)
                self.assertFalse(self.panel.isVisible())

    def test_hover_has_one_slow_rebound_and_lands_at_exact_height(self):
        curve=self.panel._hover_curve
        samples=[curve.valueForProgress(index/100) for index in range(101)]
        peak=samples.index(max(samples))
        self.assertGreater(samples[peak],1.)
        self.assertTrue(all(a<=b for a,b in zip(samples[:peak],samples[1:peak+1])))
        self.assertTrue(all(a>=b for a,b in zip(samples[peak:],samples[peak+1:])))
        self.panel._stack_frame(samples[peak])
        highest=self.panel._surface_height()
        self.panel._stack_frame(1.)
        self.assertGreater(highest,self.panel._surface_height())
        self.assertLessEqual(highest-self.panel._surface_height(),HOVER_REBOUND_PX)
        self.assertGreaterEqual(self.panel._stack.duration()*(1-peak/100),150.)

    def test_reveal_has_one_gradual_return_and_fade_cannot_recoil(self):
        self.panel.hide()
        self.panel.place(QRect(550,35,332,280),QRect(0,0,1000,850),shelf_alpha=0.)
        self.panel.follow_shelf(.62,False)
        self.panel.follow_shelf(1.,False)
        settled=self.panel._content_top()
        self.panel.follow_shelf(.985,False)
        self.assertEqual(self.panel._companion_opacity,1.)
        self.assertEqual(self.panel._content_top(),settled)
        self.panel._reveal.stop()
        offsets=[]
        for step in range(101):
            self.panel._reveal_frame(step/100)
            offsets.append(self.panel._content_top()-settled)
        peak=offsets.index(max(offsets))
        self.assertAlmostEqual(offsets[peak],5.)
        self.assertTrue(all(after<=before for before,after in zip(offsets[peak:],offsets[peak+1:])))
        self.assertAlmostEqual(offsets[-1],0.)
        self.assertGreater(self.panel._reveal.duration()*(1-peak/100),160.)

    def test_scroll_coalesces_wheel_input_and_visits_only_visible_rows(self):
        self.panel.set_entries([dict(self.rows[0],id=str(index)) for index in range(1000)])
        self.panel._finish_entries()
        self.panel.show_all()
        self.panel._stack.stop()
        self.panel._stack_frame(1.)
        self.panel.scroll=24600.
        sample=QPointF(100,180)
        with patch.object(self.panel._wheel_frames,'start'):
            for _ in range(10):
                event=QWheelEvent(sample,sample,QPoint(0,-8),QPoint(),Qt.NoButton,Qt.NoModifier,Qt.ScrollUpdate,False)
                self.panel.wheelEvent(event)
        self.assertEqual(self.panel.scroll,24600.)
        self.assertEqual(len(self.panel._wheel_events),1)
        self.panel._scroll_frame()
        self.assertEqual(self.panel.scroll,24680.)
        visible=list(self.panel._layout_rows(windowed=True))
        self.assertLess(len(visible),12)
        viewport=QRect(0,54,332,280)
        expected=[row[1]['id'] for row in self.panel._layout_rows() if row[2].intersects(viewport)]
        ids={row[1]['id'] for row in visible}
        self.assertTrue(set(expected)<=ids)

    def test_image_thumbnail_previews_and_card_body_pastes(self):
        row=dict(self.rows[0],kind='image')
        self.panel.set_entries([row])
        self.panel._finish_entries()
        opened=[];pasted=[]
        self.panel.preview_requested.connect(opened.append)
        self.panel.activated.connect(pasted.append)
        self.click('thumb',row['id'])
        self.assertEqual(opened,[row['id']])
        self.assertEqual(pasted,[])
        self.click('card',row['id'])
        self.assertEqual(pasted,[row['id']])

    def test_hover_collapse_rebounds_the_panel_edge_and_scroll_stays_in_range(self):
        self.panel.show_all()
        self.panel._stack.stop()
        self.panel._stack_frame(1.)
        self.panel.show_all(False)
        curve=self.panel._stack.easingCurve()
        peak=max(curve.valueForProgress(step/100) for step in range(101))
        self.panel._stack.stop()
        self.panel._stack_frame(1.-peak)
        self.assertLess(self.panel._surface_height(),FOLDED_HEIGHT)
        self.panel._stack_frame(0.)
        self.assertEqual(self.panel._surface_height(),FOLDED_HEIGHT)

        for delta,current,direction in ((-120.,90.,-1),(120.,0.,1)):
            with self.subTest(delta=delta):
                self.panel._reset_pull()
                landed=self.panel._scroll_motion('',current,90.,delta,Qt.ScrollUpdate)
                self.assertEqual(landed,current)
                self.assertGreater(self.panel._pull_offset*direction,0)
                self.panel._scroll_motion('',current,90.,0.,Qt.ScrollEnd)
                self.assertEqual(self.panel._pull_return.state(),self.panel._pull_return.Running)
                sample_until(lambda:self.panel._pull_offset==0.)

    def test_entry_reorder_and_delete_animate_the_existing_rows(self):
        self.panel.show_all()
        sample_until(lambda:self.panel._stack_progress>.99)
        initial={row['id']:rect.top() for _,row,rect,_,_ in self.panel._rows_at_current_frame()}
        pinned=dict(self.rows[2],pinned=True)
        self.panel.set_entries([pinned,self.rows[0],self.rows[1]])
        self.assertAlmostEqual(next(rect.top() for _,row,rect,_,_ in self.panel._rows_at_current_frame() if row['id']=='3'),
                               initial['3'],delta=1)
        sample_until(lambda:self.panel._entry_progress==1)
        self.assertEqual(next(iter(self.panel._rows_at_current_frame()))[1]['id'],'3')
        self.panel.set_entries([pinned,self.rows[1]])
        self.assertEqual([row['id'] for row,*_ in self.panel._leaving],['1'])
        sample_until(lambda:self.panel._entry_progress==1)
        self.assertFalse(self.panel._leaving)

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
    def test_history_is_enabled_for_new_profiles_and_preserves_explicit_opt_out(self):
        from app_settings import SettingsStore
        with TemporaryDirectory() as directory:
            settings=SettingsStore(directory)
            self.assertTrue(settings.clipboard_history_enabled)
            settings.update(clipboard_history_enabled=False)
            self.assertFalse(SettingsStore(directory).clipboard_history_enabled)

    def test_selected_record_leaves_current_clipboard_content_in_place(self):
        from clipboard_history_runtime import ClipboardHistory
        from types import SimpleNamespace
        from unittest.mock import ANY,Mock
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
                    insert.assert_called_once_with('历史粘贴测试','target',cancel=ANY)
                self.assertEqual(APP.clipboard().text(),'当前剪贴板测试')
                runtime.set_enabled(False)
                self.assertFalse(runtime.panel.isVisible())
                self.assertEqual(runtime.store.execute('list',12,0)[0]['title'],'历史粘贴测试')
            finally:runtime.close()


    def test_external_image_card_dispatches_image_paste_and_preserves_clipboard(self):
        from clipboard_history_runtime import ClipboardHistory
        from types import SimpleNamespace
        from unittest.mock import ANY,Mock
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
                image=QImage(9,7,QImage.Format_ARGB32)
                image.fill(QColor('#3817AA'))
                runtime.observe(('image',{'image':image}))
                sample_until(lambda:len(runtime.entries)==1)
                runtime.panel.resize(332,360);runtime.panel.show()
                APP.clipboard().setText('Keep this clipboard content')
                with patch('clipboard_history_runtime.front_target',return_value='target'), \
                     patch('clipboard_history_runtime.insert_image',return_value=True) as paste:
                    runtime.panel.activated.emit(runtime.entries[0]['id'])
                    sample_until(lambda:shelf.hide_animated.called)
                    data=runtime.store.execute('payload',runtime.entries[0]['id'])[1]
                    paste.assert_called_once_with(data,'target',cancel=ANY)
                    self.assertEqual(QImage.fromData(data).pixelColor(2,3),image.pixelColor(2,3))
                self.assertEqual(APP.clipboard().text(),'Keep this clipboard content')
            finally:
                runtime.close()


class NativeHistoryImageTests(unittest.TestCase):
    def test_native_paste_receives_image_and_restores_previous_data_or_keeps_new_copy(self):
        from clipboard_history_native import _paste_windows_image,image_paste_change
        from clipboard_history_store import encode_png
        from threading import Event
        from unittest.mock import Mock

        image=QImage(10,8,QImage.Format_ARGB32)
        image.fill(QColor('#3817AA'))
        png=encode_png(image)

        for next_copy in (False,True):
            with self.subTest(next_copy=next_copy):
                clipboard={13:896}
                sequence=600+int(next_copy)*20
                owner=501
                user=Mock()
                user.CreateWindowExW.return_value=100
                user.RegisterClipboardFormatW.side_effect={'PNG':49160,'CanIncludeInClipboardHistory':49162,
                    'CanUploadToCloudClipboard':49163}.__getitem__
                def clear():
                    nonlocal sequence,owner
                    owner=100
                    clipboard.clear()
                    sequence+=1
                    return True
                def put(fmt,handle):
                    nonlocal sequence
                    clipboard[fmt]=handle
                    sequence+=1
                    return handle
                def paste(_,target):
                    self.assertEqual(target,'target')
                    self.assertEqual(clipboard[49160],png)
                    self.assertTrue(int.from_bytes(clipboard[8][:4],'little')==40)
                    self.assertTrue(image_paste_change(sequence))
                    self.assertEqual(clipboard[49162],bytes(4))
                    self.assertEqual(clipboard[49163],bytes(4))
                    return True
                def during_paste(_):
                    nonlocal sequence,owner
                    if next_copy:
                        clear()
                        put(13,200)
                        owner=701
                    else:
                        sequence+=1  # A target materialized another clipboard format.
                def close():
                    nonlocal sequence
                    sequence+=1  # Windows finished publishing synthesized formats.
                    return True
                def backup(user,kernel,ole,snapshot):
                    snapshot.update(clipboard)
                    return True
                user.EmptyClipboard.side_effect=clear
                user.SetClipboardData.side_effect=put
                user.GetClipboardSequenceNumber.side_effect=lambda:sequence
                user.GetClipboardOwner.side_effect=lambda:owner
                user.CloseClipboard.side_effect=close
                with patch('clipboard_history_native._windows_clipboard_api',return_value=(user,Mock(),Mock(),Mock())), \
                     patch('clipboard_history_native._excluded_windows_format',return_value=49161), \
                     patch('clipboard_history_native.front_target',return_value='target'), \
                     patch('clipboard_history_native.private_clipboard',return_value=False), \
                     patch('clipboard_history_native._snapshot_windows_clipboard',side_effect=backup), \
                     patch('clipboard_history_native._windows_buffer',side_effect=lambda _,data:data), \
                     patch('clipboard_history_native._send_windows_paste',side_effect=paste), \
                     patch('clipboard_history_native.time.sleep',side_effect=during_paste):
                    self.assertTrue(_paste_windows_image(png,image,'target',Event()))
                self.assertEqual(clipboard,{13:200 if next_copy else 896})
                self.assertEqual(image_paste_change(sequence),not next_copy)


if __name__=='__main__':unittest.main()

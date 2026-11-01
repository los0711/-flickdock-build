"""Real transfer deletion entry, paint-only offsets and disposable persistence."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint,QPointF,QRect,QRectF,Qt,QEvent
from PySide6.QtTest import QTest
from PySide6.QtGui import QColor,QPainter,QEnterEvent,QMouseEvent
from PySide6.QtWidgets import QLabel,QGraphicsDropShadowEffect,QWidget
from test_html_native_workspace import APP,flush
from test_html_native_lan import FakeLan
from app_settings import SettingsStore
from lan_panel import LanDialog,_DevicesCombo,_DeviceDelegate


def synthetic_move(widget,point):
    APP.sendEvent(widget,QMouseEvent(QEvent.MouseMove,QPointF(point),QPointF(widget.mapTo(widget.window(),point)),
        QPointF(widget.mapToGlobal(point)),Qt.NoButton,Qt.NoButton,Qt.NoModifier))


class TransferRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.service=FakeLan();self.dialog=LanDialog(self.service,SettingsStore(self.base/'settings'))
        self.available=patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,1600,1000));self.available.start()
        self.motion=patch('transfer_item_motion.motion_allowed',return_value=True);self.motion.start()
        self.dialog.show();flush();self.panel=self.dialog.panel
    def tearDown(self):
        self.dialog.hide();self.dialog.deleteLater();flush();self.motion.stop();self.available.stop();self.temp.cleanup()
    def queue(self,count=6):
        paths=[]
        for index in range(count):
            path=self.base/f'original-{index}.txt';path.write_text('original bytes');paths.append(str(path))
        self.panel.queue_paths(paths,replace=True);self.panel.queue_area.animation.setCurrentTime(200);flush()
        for row in self.panel.queue_rows:row.entrance.cancel()
        return paths
    def history(self,count=4):
        self.paths=self.queue(1);self.panel.queue_paths([],replace=True);flush();self.panel.queue_motion.cancel()
        self.panel._history=[dict(name=f'transfer-{index}.txt',peer='Mac',state='完成',direction='send',when='10:30',paths=self.paths) for index in range(count)]
        self.panel._render_history();flush();self.panel._save_history()
        return list(self.panel._history)

    def test_immediate_clear_and_refill_keeps_queue_visible_when_animation_start_equals_end(self):
        paths=self.queue(3);self.assertEqual(self.panel.queue_area.value,1.)
        self.panel.clear_queue.click();self.assertEqual(self.panel._queued_paths,[])
        self.panel.queue_paths(paths,replace=True);flush()
        self.assertTrue(self.panel.queue_area.filled);self.assertEqual(self.panel._queued_paths,paths)
        self.assertFalse(self.panel.queue_column.isHidden())
        self.assertTrue(all(row.isVisible() for row in self.panel.queue_rows))
        self.assertEqual(self.service.calls,[])

    def test_dark_activity_scroll_never_paints_native_light_palette_over_live_content(self):
        self.history(2);self.dialog.apply_theme('dark');flush()
        area=self.panel.activity_scroll;image=self.dialog.grab().toImage()
        point=area.mapTo(self.dialog,area.rect().center());ratio=image.devicePixelRatio()
        color=image.pixelColor(round(point.x()*ratio),round(point.y()*ratio))
        self.assertLess(max(color.red(),color.green(),color.blue()),80)
        self.assertEqual(color.alpha(),255)
        self.assertTrue(all(row.isVisible() for row in self.panel.history_rows))
        self.assertFalse(area.viewport().autoFillBackground())

    def test_clear_history_persists_all_removals_together_without_touching_files_or_live_queue(self):
        self.history(4);p=self.panel;paths=list(self.paths);self.assertTrue(p.clear_history.isVisible())
        queue=self.queue(2);p._history_reveal_pending=(p._history[0],paths[0])
        p.clear_history.click();flush()
        self.assertEqual(p._history,[]);self.assertEqual(json.loads(p._history_path.read_text()),[])
        self.assertIsNone(p._history_reveal_pending);self.assertFalse(p.clear_history.isVisible())
        self.assertEqual(p._queued_paths,queue);self.assertTrue(all(Path(path).read_text()=='original bytes' for path in paths+queue))
        self.assertGreater(len(p.history_motion.ghosts),0)
        self.assertTrue(all(g['style']=='slide' for g in p.history_motion.ghosts))
        ghosts=p.history_motion.ghosts
        self.assertEqual([g['delay'] for g in ghosts],list(range(0,len(ghosts)*24,24)))
        p.history_motion.animation.setCurrentTime(36)
        if len(ghosts)>1:self.assertLess(p.history_motion._ghost_frame(ghosts[0])[1],p.history_motion._ghost_frame(ghosts[1])[1])
        self.assertLessEqual(p.history_motion.animation.duration(),296)
        p.history_motion.animation.setCurrentTime(p.history_motion.animation.duration());flush();self.assertEqual(p.history_motion.ghosts,[])

    def test_failed_clear_history_retains_records_buttons_and_does_not_start_visual_removal(self):
        original=self.history(2);p=self.panel
        with patch.object(p,'_save_history',side_effect=OSError('disk busy')):p.clear_history.click()
        self.assertEqual(p._history,original);self.assertTrue(p.clear_history.isVisible())
        self.assertEqual(p.history_motion.ghosts,[]);self.assertIn('无法清除',p.status_label.text())

    def test_clear_while_scrolled_keeps_staggered_ghosts_through_programmatic_range_clamp(self):
        self.history(12);p=self.panel;bar=p.history_scroll.verticalScrollBar()
        bar.setValue(bar.maximum());flush();self.assertGreater(bar.value(),0)
        p.clear_history.click();flush()
        self.assertEqual(bar.maximum(),0);self.assertEqual(bar.value(),0)
        self.assertEqual(p._history,[]);self.assertGreater(len(p.history_motion.ghosts),0)
        self.assertEqual(p.history_motion.animation.state(),p.history_motion.animation.State.Running)
        # Genuine user wheel/key/slider intent still cancels the visual work.
        bar.actionTriggered.emit(1);flush();self.assertEqual(p.history_motion.ghosts,[])

    def test_queue_remove_submits_immediately_then_reflows_without_geometry_tween(self):
        paths=self.queue();native=self.dialog.winId();window=self.dialog.geometry()
        removed=self.panel._queue_row_map[paths[0]];survivor=self.panel._queue_row_map[paths[1]]
        old=QRectF(QPoint(survivor.mapTo(self.panel,QPoint())),survivor.size())
        from lan_panel import _HistoryRemoveButton
        removed.findChild(_HistoryRemoveButton).click()
        self.assertEqual(self.panel._queued_paths,paths[1:]);self.assertEqual(len(self.panel.queue_rows),5)
        self.assertNotIn(paths[0],self.panel._queue_row_map);self.assertEqual(self.service.calls,[])
        flush();motion=self.panel.queue_motion;self.assertEqual(len(motion.ghosts),1);self.assertEqual(motion.ghosts[0]['style'],'shrink')
        self.assertTrue(motion.moves);rect=survivor.geometry();start=survivor.entrance.motion_offset
        actual=QRectF(QPoint(survivor.mapTo(self.panel,QPoint())),survivor.size())
        self.assertEqual(actual.topLeft()+start,old.topLeft())
        motion.animation.setCurrentTime(90);self.assertEqual(survivor.geometry(),rect)
        self.assertLess(abs(survivor.entrance.motion_offset.x()),abs(start.x()))
        self.assertTrue(survivor.findChild(_HistoryRemoveButton).isEnabled())
        motion.animation.setCurrentTime(motion.duration);flush()
        self.assertFalse(motion.layer.isVisible());self.assertFalse(motion.ghosts);self.assertFalse(motion.moves)
        self.assertIs(survivor.graphicsEffect(),survivor.entrance);self.assertFalse(survivor.entrance.isEnabled())
        self.assertEqual(self.dialog.geometry(),window);self.assertEqual(self.dialog.winId(),native)
        self.assertTrue(all(Path(path).read_text()=='original bytes' for path in paths))

    def test_history_delete_persists_before_animation_and_lower_rows_slide_up(self):
        records=self.history();old_rows=list(self.panel.history_rows)
        old=old_rows[1].mapTo(self.panel,QPoint());pending=records[0]
        self.panel._history_reveal_pending=(pending,self.paths[0]);old_rows[0].remove_button.click()
        self.assertEqual(self.panel._history,records[1:]);self.assertIsNone(self.panel._history_reveal_pending)
        self.assertEqual(len(json.loads(self.panel._history_path.read_text())),3)
        self.assertTrue(all(not row.isVisible() for row in old_rows));flush()
        motion=self.panel.history_motion;self.assertEqual(len(motion.ghosts),1);self.assertEqual(motion.ghosts[0]['style'],'slide')
        survivor=self.panel.history_rows[0];self.assertEqual(survivor.record_identity,id(records[1]));self.assertTrue(motion.moves)
        effect=survivor.graphicsEffect();self.assertEqual(QPointF(survivor.mapTo(self.panel,QPoint()))+effect.motion_offset,QPointF(old))
        rect=survivor.geometry();start=QRectF(motion.ghosts[0]['rect']);motion.animation.setCurrentTime(85)
        self.assertLess(motion._ghost_frame(motion.ghosts[0])[0].x(),start.x());self.assertEqual(survivor.geometry(),rect)
        motion.animation.setCurrentTime(motion.duration);self.assertIsNone(survivor.graphicsEffect());self.assertFalse(motion.layer.isVisible())
        self.assertTrue(Path(self.paths[0]).exists())

    def test_quick_queue_deletes_clear_last_item_and_do_not_duplicate_rows_or_clocks(self):
        paths=self.queue(8);motion=self.panel.queue_motion
        from lan_panel import _HistoryRemoveButton
        self.panel.queue_rows[0].findChild(_HistoryRemoveButton).click();flush();motion.animation.setCurrentTime(60)
        self.panel.queue_rows[0].findChild(_HistoryRemoveButton).click();flush()
        self.assertEqual(len(motion.ghosts),2);self.assertLessEqual(len(motion.moves),motion.max_survivors)
        self.panel.clear_queue.click();self.assertEqual(self.panel._queued_paths,[]);self.assertEqual(self.panel.queue_rows,[]);flush()
        self.assertTrue(motion.layer.isVisible());self.assertFalse(motion.moves)
        # Offscreen queue rows are removed immediately without cached animation.
        self.assertGreaterEqual(len(motion.ghosts),4);self.assertLessEqual(len(motion.ghosts),8)
        self.assertTrue(all(not child.isVisible() for child in self.panel.queue_body.findChildren(QWidget) if child.objectName()=='lanQueueRow'))
        motion.animation.setCurrentTime(motion.duration);flush();self.assertFalse(motion.ghosts);self.assertFalse(motion.layer.isVisible())
        self.assertTrue(all(Path(path).exists() for path in paths))

    def test_rapid_history_deletes_cancel_pending_reveal_and_leave_no_live_old_effects(self):
        self.history(6);motion=self.panel.history_motion
        for _ in range(6):
            self.panel.history_rows[0].remove_button.click();flush()
            if motion.animation.state()==motion.animation.State.Running:motion.animation.setCurrentTime(30)
        self.assertEqual(self.panel._history,[]);self.assertEqual(self.panel.history_rows,[])
        self.assertEqual(json.loads(self.panel._history_path.read_text()),[])
        self.assertLessEqual(len(motion.ghosts),6);motion.animation.setCurrentTime(motion.duration);flush()
        self.assertFalse(motion.moves);self.assertFalse(motion.ghosts);self.assertFalse(motion.layer.isVisible())

    def test_failed_history_save_retains_record_and_does_not_start_animation(self):
        records=self.history();rows=list(self.panel.history_rows)
        with patch.object(self.panel,'_save_history',side_effect=OSError('disk unavailable')):rows[0].remove_button.click()
        self.assertEqual(self.panel._history,records);self.assertEqual(self.panel.history_rows,rows)
        self.assertFalse(self.panel.history_motion.ghosts);self.assertFalse(self.panel.history_motion.moves)

    def test_hidden_reduced_motion_and_scroll_clear_visual_work_without_data_delay(self):
        paths=self.queue();self.panel.queue_paths(paths[1:],replace=True);flush()
        self.panel.queue_scroll.verticalScrollBar().setValue(1)
        self.assertFalse(self.panel.queue_motion.ghosts)
        self.dialog.hide();self.panel.queue_paths(paths[2:],replace=True);flush()
        self.assertEqual(self.panel._queued_paths,paths[2:]);self.assertFalse(self.panel.queue_motion.animation.state()==self.panel.queue_motion.animation.State.Running)
        self.dialog.show();flush()
        with patch('transfer_item_motion.motion_allowed',return_value=False):self.panel.queue_paths([],replace=True);flush()
        self.assertEqual(self.panel._queued_paths,[]);self.assertFalse(self.panel.queue_motion.ghosts);self.assertFalse(self.panel.queue_motion.moves)

    def test_foreign_effect_is_preserved_and_hundred_deletes_do_not_accumulate_timers(self):
        paths=self.queue(2);survivor=self.panel._queue_row_map[paths[1]];shadow=QGraphicsDropShadowEffect(survivor);survivor.setGraphicsEffect(shadow)
        self.panel.queue_paths(paths[1:],replace=True);flush();self.assertIs(survivor.graphicsEffect(),shadow)
        self.panel.queue_motion.animation.setCurrentTime(200);flush()
        from PySide6.QtCore import QTimer,QVariantAnimation
        clocks=len(self.panel.queue_motion.findChildren(QVariantAnimation));timers=len(self.panel.queue_motion.findChildren(QTimer))
        for _ in range(100):
            self.panel.queue_paths(paths,replace=True);flush()
            self.panel.queue_paths([],replace=True);flush();self.panel.queue_motion.animation.setCurrentTime(200)
        flush();self.assertFalse(self.panel.queue_motion.moves);self.assertFalse(self.panel.queue_motion.ghosts)
        self.assertEqual(len(self.panel.queue_motion.findChildren(QVariantAnimation)),clocks);self.assertEqual(len(self.panel.queue_motion.findChildren(QTimer)),timers)

    def test_title_status_elides_without_losing_full_text_and_history_actions_are_square(self):
        self.history(1)
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme);message='远程传输就绪 · '+('完整状态说明 '*30);label=self.panel.status_label;label.setText(message);flush()
            self.assertEqual(label.parentWidget(),self.dialog.title_bar);self.assertEqual(label.text(),message)
            self.assertEqual(label.accessibleName(),message);self.assertEqual(label.toolTip(),message)
            self.assertLess(len(QLabel.text(label)),len(message));self.assertIn('…',QLabel.text(label))
            self.assertGreater(label.geometry().left(),self.dialog.back_button.geometry().right())
            self.assertLessEqual(label.geometry().right(),self.dialog.title_bar.rect().right())
            if self.dialog.close_button.isVisible():self.assertLessEqual(label.geometry().right(),self.dialog.close_button.mapTo(self.dialog.title_bar,QPoint()).x())
            row=self.panel.history_rows[0];self.assertEqual(row.reveal_button.size(),row.remove_button.size())
            self.assertEqual(row.remove_button.size().toTuple(),(28,28));self.assertEqual(self.dialog.mode_bar.height(),50)
            self.assertIn('border:1px',self.panel.styleSheet())
            back_rect=self.dialog.back_button.geometry();label.clear();flush()
            self.assertEqual(self.dialog.back_button.geometry(),back_rect)

    def test_two_equal_activity_groups_and_numeric_capsule_survive_both_modes_on_short_screen(self):
        from wan_transfer import WanTransferService
        wan=WanTransferService(self.base/'wan',enabled=False)
        try:
            for service in (self.service,wan):
                panel=self.dialog.set_service(service)
                for index in range(2):panel.show_offer(dict(id=str(index),peer='Mac',count=1,names=['original.zip'],total=1234,code='123456'))
                panel._history=[dict(name=f'file-{index}',peer='Mac',state='完成',direction='send',when='10:30',paths=[]) for index in range(12)]
                panel._render_history()
                for theme in ('light','dark'):
                    self.dialog.apply_theme(theme)
                    for size in ((980,792),(824,660)):
                        self.dialog.resize(*size)
                        for _ in range(8):flush()
                        self.assertLessEqual(abs(panel.active_group.height()-panel.recent_group.height()),1)
                        self.assertEqual(panel.transfer_count.text(),'2');self.assertEqual(panel.transfer_count.accessibleName(),'2 项进行中')
                        self.assertEqual(panel.transfer_count.height(),22);self.assertGreater(panel.transfer_count.width(),20)
                        last=panel.history_rows[-1]
                        panel.activity_scroll.ensureWidgetVisible(panel.history_scroll,0,0)
                        panel.history_scroll.ensureWidgetVisible(last.remove_button,0,0)
                        for _ in range(3):flush()
                        for area in (panel.activity_scroll,panel.history_scroll):
                            self.assertTrue(area.viewport().rect().contains(last.remove_button.mapTo(area.viewport(),QPoint())))
                            self.assertTrue(area.viewport().rect().contains(last.remove_button.mapTo(area.viewport(),last.remove_button.rect().bottomRight())))
        finally:wan.close();wan.deleteLater();flush()


class DeviceRemoveTests(unittest.TestCase):
    def test_delete_hover_is_inset_and_mouse_release_does_not_activate_device(self):
        owner=QWidget();owner.resize(360,100);combo=_DevicesCombo(owner);combo.resize(320,36);combo.removable=True
        delegate=_DeviceDelegate(combo);delegate.removable=True;combo.setItemDelegate(delegate)
        combo.addItem('Mac','mac');combo.addItem('Windows','win');owner.show();combo.showPopup();flush()
        try:
            view=combo.view();index=combo.model().index(1,0);rect=view.visualRect(index);hit=delegate.remove_rect(rect)
            self.assertEqual(rect.right()-hit.right(),16);self.assertEqual(hit.width(),28)
            deleted=[];activated=[];combo.remove_requested.connect(deleted.append);combo.activated.connect(activated.append)
            viewport=view.viewport();point=hit.center();viewport.setAttribute(Qt.WA_UnderMouse,True)
            APP.sendEvent(viewport,QEnterEvent(QPointF(point),QPointF(viewport.mapTo(viewport.window(),point)),QPointF(viewport.mapToGlobal(point))))
            synthetic_move(viewport,point);flush();self.assertEqual(delegate.remove_hover_id,'win')
            plain=view.viewport().grab().toImage();delegate.remove_hover_id=None;view.viewport().update();flush();normal=view.viewport().grab().toImage();self.assertNotEqual(plain,normal)
            synthetic_move(view.viewport(),hit.center());QTest.mouseClick(view.viewport(),Qt.LeftButton,Qt.NoModifier,hit.center());flush()
            self.assertEqual(deleted,['win']);self.assertEqual(activated,[]);self.assertEqual(combo.currentData(),'mac')
            QTest.mousePress(view.viewport(),Qt.LeftButton,Qt.NoModifier,hit.center());QTest.mouseRelease(view.viewport(),Qt.LeftButton,Qt.NoModifier,QPoint(rect.left()+60,rect.center().y()));flush()
            self.assertEqual(deleted,['win']);self.assertEqual(activated,[]);self.assertEqual(combo.currentData(),'mac')
        finally:
            APP.sendEvent(combo.view().viewport(),QEvent(QEvent.Leave));combo.view().viewport().setAttribute(Qt.WA_UnderMouse,False)
            combo.hidePopup();owner.hide();owner.deleteLater();flush()


class _Solid(QWidget):
    def __init__(self,color,parent):super().__init__(parent);self.color=QColor(color)
    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),self.color);painter.end()


class TransferEffectPixelTests(unittest.TestCase):
    def setUp(self):
        self.root=QWidget();self.root.setStyleSheet('background:white');self.root.resize(300,180);self.root.show();flush()
    def tearDown(self):self.root.hide();self.root.deleteLater();flush()
    def pixel(self,x,y):
        pixmap=self.root.grab();ratio=pixmap.devicePixelRatioF()
        return pixmap.toImage().pixelColor(round(x*ratio),round(y*ratio)).getRgb()[:3]

    def test_scale_fade_actually_composites_alpha_and_scale_pixels(self):
        from transfer_drop_feedback import ScaleFade
        row=_Solid('#ff0000',self.root);row.setGeometry(20,20,200,50);row.show();effect=ScaleFade(row);effect.setEnabled(True)
        geometry=row.geometry()
        effect._frame(0.);flush();self.assertEqual(self.pixel(100,40),(255,255,255))
        effect._frame(.5);flush();half=self.pixel(100,40)
        self.assertEqual(half[0],255);self.assertAlmostEqual(half[1],128,delta=2);self.assertEqual(half[1],half[2])
        effect._frame(1.);flush();self.assertEqual(self.pixel(100,40),(255,0,0))
        effect.initial_opacity=1.;effect._frame(0.);flush()
        self.assertEqual(self.pixel(21,40),(255,255,255));self.assertEqual(self.pixel(26,40),(255,0,0))
        effect._frame(1.);flush();self.assertEqual(self.pixel(21,40),(255,0,0));self.assertEqual(row.geometry(),geometry)

    def _assert_survivor_pixels(self,borrowed):
        from transfer_item_motion import TransferItemMotion
        from transfer_drop_feedback import ScaleFade
        viewport=QWidget(self.root);viewport.setGeometry(20,20,260,140);viewport.show()
        removed=_Solid('#ff0000',viewport);removed.setGeometry(0,0,80,30);removed.show()
        survivor=_Solid('#00ff00',viewport);survivor.setGeometry(100,0,80,30);survivor.show()
        if borrowed:survivor.entrance=ScaleFade(survivor)
        flush();controller=TransferItemMotion(self.root,viewport)
        with patch('transfer_item_motion.motion_allowed',return_value=True):
            positions=controller.capture({'removed':removed,'survivor':survivor})
            controller.remove({'removed':removed},positions,style='shrink')
            removed.hide();survivor.setGeometry(0,0,80,30) # Final layout committed before animation.
            geometry=survivor.geometry();controller.reflow({'survivor':survivor},positions);flush()
            def green_left():
                pixmap=self.root.grab();image=pixmap.toImage();ratio=pixmap.devicePixelRatioF()
                xs=[x for x in range(300) if image.pixelColor(round(x*ratio),round(35*ratio)).getRgb()[:3]==(0,255,0)]
                self.assertTrue(xs);return min(xs)
            controller.animation.setCurrentTime(0);self.assertEqual(green_left(),120)
            controller.animation.setCurrentTime(80);middle=green_left();self.assertGreater(middle,20);self.assertLess(middle,120)
            self.assertEqual(survivor.geometry(),geometry)
            controller.animation.setCurrentTime(200);flush();self.assertEqual(green_left(),20);self.assertEqual(survivor.geometry(),geometry)
            if borrowed:self.assertIs(survivor.graphicsEffect(),survivor.entrance);self.assertFalse(survivor.entrance.isEnabled())
            else:self.assertIsNone(survivor.graphicsEffect())

    def test_history_survivor_offset_moves_real_pixels_without_moving_layout(self):self._assert_survivor_pixels(False)
    def test_queue_borrowed_entrance_offset_moves_real_pixels_without_moving_layout(self):self._assert_survivor_pixels(True)


if __name__=='__main__':unittest.main()

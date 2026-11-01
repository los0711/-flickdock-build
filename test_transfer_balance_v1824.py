"""Actual transfer activity geometry; offscreen and disposable files only."""
import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QPoint,QRect
from PySide6.QtWidgets import QWidget,QLabel
from shiboken6 import isValid
from test_html_native_workspace import APP,flush
from test_html_native_lan import FakeLan
from app_settings import SettingsStore
from lan_panel import LanDialog

REPORT=[]

class ActivityBalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.available=patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,1600,1000));self.available.start()
        self.reduce=patch('native_ui_common.reduced_motion',return_value=True);self.reduce.start()
        self.store=SettingsStore(self.base);self.service=FakeLan()
        records=[dict(name=f'Completed-{i}.bin',peer='QA device',state='完成',direction='send',when='11-01 12:30',paths=[]) for i in range(12)]
        (self.base/'lan_history.json').write_text(json.dumps(records))
        self.dialog=LanDialog(self.service,self.store);self.dialog.show();self.dialog.resize(980,704);self.panel=self.dialog.panel;self.settle()
    def tearDown(self):
        self.dialog.hide();self.dialog.deleteLater();flush();self.reduce.stop();self.available.stop();self.temp.cleanup()
    def settle(self):
        for _ in range(8):flush()
    def bounds(self,w,owner):return QRect(w.mapTo(owner,QPoint()),w.size())
    def record(self,label):
        p=self.panel;a=p.activity_scroll;bar=p.history_scroll.verticalScrollBar()
        row=dict(label=label,theme=self.store.theme,dialog=list(self.dialog.size().toTuple()),
            active=list(self.bounds(p.active_group,p.activity_column).getRect()),
            recent=list(self.bounds(p.recent_group,p.activity_column).getRect()),
            activity_body=p.activity_column.height(),activity_viewport=a.viewport().height(),
            outer_scroll=[a.verticalScrollBar().value(),a.verticalScrollBar().maximum()],
            receive=list(self.bounds(p.receive_path,a.viewport()).getRect()),
            history_scroll=[bar.value(),bar.maximum()],history_rows=[id(w) for w in p.history_rows])
        row['sizing']={name:dict(minimum=list(w.minimumSize().toTuple()),min_hint=list(w.minimumSizeHint().toTuple()),hint=list(w.sizeHint().toTuple()),policy=w.sizePolicy().verticalPolicy().name,layout_min=list(w.layout().minimumSize().toTuple()) if w.layout() else None) for name,w in [('active',p.active_group),('recent',p.recent_group),('body',p.activity_column),('history_scroll',p.history_scroll)]}
        REPORT.append(row);return row
    def equal(self):self.assertLessEqual(abs(self.panel.active_group.height()-self.panel.recent_group.height()),1)
    def test_twelve_history_records_do_not_expand_recent_or_push_destination_outside_default_view(self):
        for theme in ('light','dark'):
            self.store.update(theme=theme);self.dialog.apply_theme(theme);self.settle();row=self.record('empty-queue-12-history')
            self.equal();p=self.panel
            self.assertEqual(len(p.history_rows),12)
            self.assertTrue(p.activity_scroll.viewport().rect().contains(self.bounds(p.receive_path,p.activity_scroll.viewport())),row)
            # The original 176px shared minimum plus original padding needs
            # 436px; this 420px viewport may scroll its final 16px of padding.
            minimum=p.activity_column.minimumHeight()
            self.assertEqual(p.activity_scroll.verticalScrollBar().maximum(),max(0,minimum-p.activity_scroll.viewport().height()),row)
            path=self.base/'queued-original.bin';path.write_bytes(b'original')
            p.queue_paths([str(path)]);p.queue_area.animation.setCurrentTime(200);self.settle();self.record('one-queued-12-history');self.equal()
            self.assertEqual(path.read_bytes(),b'original');p.queue_paths([],replace=True);self.settle()
            self.dialog.resize(980,840);self.settle();self.equal()
            self.assertEqual(p.activity_scroll.verticalScrollBar().maximum(),0)
            self.dialog.resize(980,704);self.settle()
    def test_deletion_preserves_survivors_scroll_and_both_track_heights(self):
        p=self.panel;bar=p.history_scroll.verticalScrollBar();bar.setValue(bar.maximum()//2);self.settle()
        before=self.record('before-delete');survivors=list(p.history_rows[1:]);texts=[w.detail.text() for w in survivors]
        p.history_rows[0].remove_button.click();self.settle();after=self.record('after-delete')
        self.equal();self.assertEqual(p.history_rows,survivors)
        self.assertTrue(all(isValid(w) for w in survivors));self.assertEqual([w.detail.text() for w in survivors],texts)
        self.assertEqual(before['active'],after['active']);self.assertEqual(before['recent'],after['recent'])
        self.assertEqual(bar.value(),min(before['history_scroll'][0],bar.maximum()))
    def test_real_incoming_offers_raise_both_minima_without_clipping_active_controls(self):
        p=self.panel
        for i in range(2):p.show_offer(dict(id=f'offer{i}',peer='QA Mac',count=1,names=['original.zip'],total=1024,code='123456'))
        for size in ((980,704),(760,560)):
            self.dialog.resize(*size);self.settle();row=self.record('two-offers')
            self.equal();self.assertGreater(p.active_group.minimumHeight(),176)
            for entry in p._rows.values():
                frame=entry['frame']
                if not frame.isVisible():continue
                self.assertTrue(p.transfers_body.rect().contains(frame.geometry()),row)
                for control in frame.findChildren(QWidget):
                    if control.isVisible():self.assertTrue(frame.rect().contains(self.bounds(control,frame)),(control.objectName(),row))
            outer=p.activity_scroll.verticalScrollBar();outer.setValue(outer.maximum());self.settle()
            self.assertTrue(p.activity_scroll.viewport().rect().contains(self.bounds(p.receive_path,p.activity_scroll.viewport())),row)

    def test_wrapped_active_content_remeasures_width_then_shrinks_when_removed(self):
        p=self.panel;p.show_offer(dict(id='wrapped',peer='QA',count=1,names=['original.zip'],total=1024,code='123456'))
        label=next(w for w in p._rows['wrapped']['frame'].findChildren(QLabel,'lanOfferMeta') if type(w) is QLabel)
        label.setText('收到传输请求，请核对发送方设备信息后选择保存位置。'*4)
        self.dialog.resize(1320,840);self.settle();wide=p.active_group.minimumHeight();self.equal()
        self.dialog.resize(760,560);self.settle();narrow=p.active_group.minimumHeight();self.record('wrapped-active-narrow');self.equal()
        self.assertGreater(narrow,wide)
        frame=p._rows['wrapped']['frame'];self.assertTrue(frame.rect().contains(self.bounds(label,frame)))
        p._rows['wrapped']['finished']=True;p._refresh_transfer_pages();self.settle()
        self.record('wrapped-active-removed');self.equal();self.assertEqual(p.active_group.minimumHeight(),176)

if __name__=='__main__':
    try:unittest.main(verbosity=2)
    finally:
        if os.environ.get('FD_BALANCE_REPORT'):Path(os.environ['FD_BALANCE_REPORT']).write_text(json.dumps(REPORT,indent=2))

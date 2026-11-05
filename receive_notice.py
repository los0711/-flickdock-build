"""One nonactivating card for authenticated same-account file reception."""
from pathlib import Path
from PySide6.QtCore import QObject,QPoint,Qt,QTimer
from PySide6.QtGui import QCursor,QGuiApplication
from PySide6.QtWidgets import QFrame,QHBoxLayout,QLabel,QProgressBar,QVBoxLayout,QWidget
from async_images import ImageCache
from flickdock_icons import app_icon
from native_ui_common import colors
from platform_native import keep_tool_visible_when_inactive
from reference_widgets import button,refresh_tree
from transfer_visuals import TransferRateTracker
from transfer_workbench import FileArtwork,reveal_file
from ui_transitions import WindowFade,fade_hide

NOTICE_WIDTH_PX=390
NOTICE_MARGIN_PX=16
COMPLETE_NOTICE_MS=4500
FAILURE_NOTICE_MS=6500
MAX_NOTICE_RECORDS=64
TERMINAL={'complete','error','cancelled','interrupted'}

class ReceiveToast(QWidget):
    def __init__(self,owner):
        super().__init__();self.owner=owner;self.record=None;self._theme=None
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        keep_tool_visible_when_inactive(self);self.setWindowTitle('FlickDock · 文件接收')
        root=QVBoxLayout(self);root.setContentsMargins(0,0,0,0);self.card=QFrame(self);self.card.setObjectName('receiveNotice');root.addWidget(self.card)
        layout=QVBoxLayout(self.card);layout.setContentsMargins(16,13,16,11);layout.setSpacing(10)
        head=QHBoxLayout();head.setSpacing(9);logo=QLabel(self.card);logo.setPixmap(app_icon().pixmap(23,23));logo.setFixedSize(23,23);head.addWidget(logo)
        self.heading=QLabel(self.card);self.heading.setObjectName('receiveHeading');self.heading.setTextFormat(Qt.PlainText);head.addWidget(self.heading,1)
        close=button('','close',parent=self.card);close.setFixedSize(24,24);close.setAccessibleName('关闭提示，传输继续');close.setToolTip('关闭提示，传输继续');close.clicked.connect(owner.dismiss);head.addWidget(close);self.dismiss_button=close
        layout.addLayout(head);device=QHBoxLayout();self.device=QLabel(self.card);self.device.setTextFormat(Qt.PlainText);device.addWidget(self.device,1)
        self.account_badge=QLabel('同账号',self.card);self.account_badge.setObjectName('receiveAccount');device.addWidget(self.account_badge);layout.addLayout(device)
        row=QHBoxLayout();row.setSpacing(10);self.artwork_cache=ImageCache(self);self.artwork=FileArtwork(self.artwork_cache,self.card);row.addWidget(self.artwork)
        info=QVBoxLayout();info.setSpacing(2);self.filename=QLabel(self.card);self.filename.setObjectName('receiveFilename');self.filename.setTextFormat(Qt.PlainText);info.addWidget(self.filename)
        self.amount=QLabel(self.card);info.addWidget(self.amount);row.addLayout(info,1);self.percentage=QLabel(self.card);row.addWidget(self.percentage);layout.addLayout(row)
        self.progress=QProgressBar(self.card);self.progress.setRange(0,1000);self.progress.setFixedHeight(4);self.progress.setTextVisible(False);layout.addWidget(self.progress)
        foot=QHBoxLayout();self.detail=QLabel(self.card);self.detail.setTextFormat(Qt.PlainText);foot.addWidget(self.detail,1)
        self.action_button=button('查看传输',parent=self.card);self.action_button.setFixedHeight(26);self.action_button.clicked.connect(owner.activate);foot.addWidget(self.action_button);layout.addLayout(foot)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(owner.dismiss)
        self._window_fade=WindowFade(self)

    def present(self,record,*,reveal=False):
        self.record=record;theme=self.owner.manager.settings.theme;c=colors(theme)
        if self._theme!=theme:
            self._theme=theme
            self.card.setStyleSheet(f'QFrame#receiveNotice{{background:{c["surface"]};border:0;border-radius:12px;}} QLabel{{background:transparent;border:0;color:{c["secondary"]};font-size:11px;}} QLabel#receiveHeading{{color:{c["text"]};font-size:13px;font-weight:600;}} QLabel#receiveFilename{{color:{c["text"]};font-size:12px;font-weight:500;}} QLabel#receiveAccount{{color:{c["green"]};padding:1px 5px;background:{c["control"]};border-radius:4px;font-size:10px;}} QProgressBar{{background:{c["control"]};border:0;border-radius:2px;}} QProgressBar::chunk{{background:{c["link"]};border-radius:2px;}}')
            refresh_tree(self);self.artwork.dark=theme=='dark'
        state=record['state'];complete=state=='complete';failed=state in ('error','cancelled','interrupted')
        title={'complete':'接收完成','error':'接收失败','cancelled':'接收已取消','interrupted':'接收已中断','verifying':'正在完成'}.get(state,'正在下载'+(f' {record["count"]} 个文件' if record['count']>1 else ''))
        self.heading.setText(title);self.heading.setStyleSheet('color:'+c['green' if complete else 'red' if failed else 'text']+';font-size:13px;font-weight:600;')
        self.device.setText(self.device.fontMetrics().elidedText('来自 '+record['peer'],Qt.ElideRight,260));self.device.setToolTip(record['peer'])
        name=record['name'] or '正在接收文件';self.filename.setText(self.filename.fontMetrics().elidedText(name,Qt.ElideMiddle,245));self.filename.setToolTip(name)
        paths=record.get('paths',[]);source=paths[0] if complete and paths else ''
        if (self.artwork.ref,self.artwork.name)!=(source,name):self.artwork.set_source(source,name)
        from lan_panel import _size
        received,total=record['received'],record['total'];fraction=min(1.,received/total) if total else 0.
        self.amount.setText((_size(total)+' · 已保存并校验') if complete else f'{_size(received)} / {_size(total)}')
        self.percentage.setText('100%' if complete else f'{fraction:.0%}' if total else '—');self.progress.setValue(1000 if complete else round(fraction*1000));self.progress.setVisible(not failed)
        route={'lan':'局域网','direct':'P2P 直连','p2p':'P2P 直连','relay':'TURN 中继','cloud':'云端'}.get(record.get('route'),'建立连接中')
        rate=self.owner.rates.bytes_per_second(record['id']);eta=self.owner.rates.remaining_text(record['id'],dict(received=received,total=total))
        detail=record.get('message') or ('文件已保存到接收位置' if complete else ' · '.join(part for part in (route,_size(rate)+'/s' if rate else '',eta if rate else '') if part))
        self.detail.setText(self.detail.fontMetrics().elidedText(detail,Qt.ElideRight,260));self.detail.setToolTip(detail)
        self.action_button.setText('打开文件夹' if complete else '查看传输');self.action_button.setEnabled(not complete or bool(paths))
        self.dismiss_button.setToolTip('关闭提示' if state in TERMINAL else '关闭提示，传输继续')
        if reveal:
            screen=QGuiApplication.screenAt(QCursor.pos()) or self.owner.manager.shelf.screen() or QGuiApplication.primaryScreen()
            area=screen.availableGeometry();self.setFixedWidth(min(NOTICE_WIDTH_PX,max(240,area.width()-2*NOTICE_MARGIN_PX)));self.adjustSize()
            self.move(area.right()-self.width()-NOTICE_MARGIN_PX+1,area.top()+NOTICE_MARGIN_PX)
            self.timer.stop();self._window_fade.reopen();self.show();self.raise_()
            if state in TERMINAL:self.timer.start(COMPLETE_NOTICE_MS if complete else FAILURE_NOTICE_MS)

    def enterEvent(self,event):self.timer.stop();super().enterEvent(event)
    def leaveEvent(self,event):
        if self.record and self.record['state'] in TERMINAL:self.timer.start(COMPLETE_NOTICE_MS)
        super().leaveEvent(event)
    def hideEvent(self,event):self.timer.stop();super().hideEvent(event)

class SameAccountReceiveNotice(QObject):
    def __init__(self,manager,service):
        super().__init__(manager.app);self.manager=manager;self.service=service;self.records={};self.current=None;self.toast=None;self.closed=False
        self.rates=TransferRateTracker()
        service.progress.connect(self._progress);service.completed.connect(self._completed)
        manager.account.account_changed.connect(self.reset);manager.settings.changed.connect(self._refresh)

    def begin(self,offer):
        if self.closed or offer['id'] in self.records:return
        names=offer.get('names') or []
        record=dict(id=offer['id'],peer=str(offer['peer'])[:160],count=offer['count'],name=Path(str(names[0])).name if names else '',
            total=offer['total'],received=0,state='connecting',route='',paths=[],dismissed=False,announced=False,snapshot=None)
        record['snapshot']=dict(id=record['id'],direction='receive',peer=record['peer'],total=record['total'],received=0,state='connecting',name=record['name'])
        self.records[record['id']]=record
        while len(self.records)>MAX_NOTICE_RECORDS:
            old=next((key for key,value in self.records.items() if value['state'] in TERMINAL and key!=self.current),None)
            if old is None:break
            self.records.pop(old)
        self.current=record['id'];self._present(record,True)

    def _present(self,record,reveal=False):
        if self.toast is None:self.toast=ReceiveToast(self)
        self.toast.present(record,reveal=reveal)

    def _progress(self,value):
        if self.closed or value.get('direction')!='receive':return
        record=self.records.get(value['id'])
        if record is None or record['announced']:return
        record.update(snapshot=dict(value),received=value.get('received',record['received']),total=value.get('total',record['total']))
        if value.get('name'):record['name']=Path(str(value['name'])).name
        if value.get('route'):record['route']=value['route']
        previous=record['state'];state=value.get('state','');record['state']='verifying' if state=='complete' else state
        if state in ('error','cancelled','interrupted'):record['message']=str(value.get('message') or '')[:600]
        else:record['message']=''
        self.rates.observe({key:dict(received=row['received'],total=row['total'],phase=row['state']) for key,row in self.records.items() if not row['announced']})
        if record['state'] in TERMINAL and previous!=record['state']:
            record['announced']=state!='interrupted';self.current=record['id'];self._present(record,True)
        elif self.current==record['id'] and not record['dismissed']:self._present(record)

    def _completed(self,value):
        if self.closed or value.get('direction')!='receive':return
        record=self.records.get(value['id'])
        if record is None or record['announced']:return
        record.update(state='complete',paths=list(value.get('paths') or []),announced=True,received=value.get('total',record['total']),message='')
        self.current=record['id'];self._present(record,True)

    def dismiss(self):
        if self.current in self.records:self.records[self.current]['dismissed']=True
        if self.toast:fade_hide(self.toast)

    def activate(self):
        record=self.records.get(self.current)
        if record is None:return
        if record['state']=='complete':
            try:reveal_file(record['paths'][0])
            except (OSError,IndexError) as error:record['message']=str(error);self._present(record)
        else:
            self.manager.show_wan_dialog();self.manager.wan_dialog.panel.focus_transfer(record['id'],record['snapshot'])

    def _refresh(self):
        if self.toast and self.toast.isVisible() and self.current in self.records:self._present(self.records[self.current])

    def reset(self,*_):
        self.records.clear();self.current=None;self.rates=TransferRateTracker()
        if self.toast:self.toast.hide()

    def close(self):
        self.closed=True;self.reset()
        if self.toast:self.toast.artwork_cache.close();self.toast.deleteLater();self.toast=None

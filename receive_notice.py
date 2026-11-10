"""Manual file-transfer feedback shares the small, nonactivating capsule host."""
from pathlib import Path
from PySide6.QtCore import QObject,QPoint,QRectF
from PySide6.QtGui import QCursor,QGuiApplication
from async_images import ImageCache
from intake_notice import CapsuleNotice,COMPACT_SIDE_PX,NOTICE_PROGRESS_DURATION_MS,NOTICE_DURATION_MS,COMPACT_SUCCESS_DURATION_MS,paint_circular_artwork
from native_ui_common import icon
from flickdock_icons import document_pixmap
from transfer_visuals import draw_ring

TRANSFER_HOST_WIDTH_PX=320
TRANSFER_CAPSULE_MAX_WIDTH_PX=296
SEND_RING_COLOR='#26B97A'
RECEIVE_RING_COLOR='#087CEF'
MAX_NOTICE_RECORDS=64
TERMINAL={'complete','error','cancelled','interrupted'}


class TransferCapsule(CapsuleNotice):
    HOST_WIDTH=TRANSFER_HOST_WIDTH_PX
    MAX_CAPSULE_WIDTH=TRANSFER_CAPSULE_MAX_WIDTH_PX
    NOTICE_ORDER=1

    def __init__(self,owner):
        super().__init__(owner.manager);self.owner=owner;self.record=None
        self.setWindowTitle('FlickDock · 文件传输提示')

    def _sync_accessibility(self):
        self.setAccessibleName(self._text)
        self.setAccessibleDescription(self._description+('；点击展开提示' if self._compact else '；点击查看文件传输'))

    def present(self,record,*,reveal=False):
        same=self.record is not None and self.record['id']==record['id'];previous=self._phase
        self.record=dict(record);state=record['state'];self._text=record['name'] or '文件'
        sending=record['direction']=='send';self._description=('发送' if sending else '接收')+('完成' if state=='complete' else '未完成' if state in TERMINAL else '文件中')
        self._phase='success' if state=='complete' else 'error' if state in TERMINAL else 'syncing'
        progress=dict(phase=state,received=record['received'],total=record['total'])
        if state in ('connecting','waiting','preparing','reconnecting'):progress=dict(phase='preparing',received=0,total=0)
        self._set_progress(progress,reset=not same)
        if not same or not self.isVisible():
            screen=QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            self._begin_presentation(screen.availableGeometry());self.timer.start(NOTICE_PROGRESS_DURATION_MS if self._phase=='syncing' else NOTICE_DURATION_MS)
        elif previous!=self._phase:
            self.timer.start(NOTICE_PROGRESS_DURATION_MS if self._phase=='syncing' else COMPACT_SUCCESS_DURATION_MS if self._compact else NOTICE_DURATION_MS)
        self._sync_accessibility();self._sync_ring_clock();self.update()

    def _paint_leading(self,painter,rect):
        paths=self.record.get('paths',[]) if self.record else [];name=self.record['name'] if self.record else ''
        path=paths[0] if paths else ''
        image=self.owner.images.image(path,64) if path else None
        if path and image is None:self.owner.images.request(path,64)
        artwork=image if image is not None and not image.isNull() else document_pixmap(Path(name).suffix or 'file',24,dpr=self.devicePixelRatioF()).toImage()
        paint_circular_artwork(painter,rect,artwork,self.manager.settings.theme,self._artwork_cache)

    def _paint_indicator(self,painter,rect,colors):
        color=SEND_RING_COLOR if self.record['direction']=='send' else RECEIVE_RING_COLOR
        if self._phase=='syncing':draw_ring(painter,rect,self._painted_progress(),color=color)
        else:icon('circle-check' if self._phase=='success' else 'circle-alert',color if self._phase=='success' else colors['red']).paint(painter,rect.toAlignedRect())

    def _expire(self):
        if self._phase=='syncing':
            self.timer.stop();self._compact=True;self._animate_shape(COMPACT_SIDE_PX);self._sync_accessibility()
        else:self.dismiss()

    def _activate(self):
        if self._compact:
            self._compact=False;self._animate_shape(self._full_width)
            self.timer.start(NOTICE_PROGRESS_DURATION_MS if self._phase=='syncing' else NOTICE_DURATION_MS);self._sync_accessibility()
        else:self.owner.activate()


class FileTransferNotices(QObject):
    def __init__(self,manager,service):
        super().__init__(manager.app);self.manager=manager;self.service=service;self.records={};self.offers={};self.current=None;self.toast=None;self.closed=False
        self.images=ImageCache(self);self.images.ready.connect(self._image_ready)
        service.offer_received.connect(self._offered);service.progress.connect(self._progress);service.completed.connect(self._completed)
        manager.account.account_changed.connect(self.reset);manager.settings.changed.connect(self._refresh)

    def _offered(self,offer):
        if self.closed or offer['id'] in self.records:return
        self.offers[offer['id']]=dict(offer)
        while len(self.offers)>MAX_NOTICE_RECORDS:self.offers.pop(next(iter(self.offers)))

    def begin(self,offer):
        if self.closed or offer['id'] in self.records:return
        names=offer.get('names') or [];direction=offer.get('direction','receive')
        record=dict(id=offer['id'],direction=direction,peer=str(offer.get('peer',''))[:160],count=offer.get('count',len(names)),
            name=Path(str(names[0])).name if names else '',total=offer.get('total',0),received=0,state='connecting',
            route='' if self.service is self.manager.wan else 'lan',paths=list(offer.get('paths') or []),announced=False,snapshot=None)
        record['snapshot']=dict(id=record['id'],direction=direction,peer=record['peer'],total=record['total'],received=0,state='connecting',name=record['name'])
        self.records[record['id']]=record;self.offers.pop(record['id'],None)
        while len(self.records)>MAX_NOTICE_RECORDS:
            old=next((key for key,value in self.records.items() if value['state'] in TERMINAL and key!=self.current),None)
            if old is None:break
            self.records.pop(old)
        self.current=record['id'];self._present(record,True)

    def _present(self,record,reveal=False):
        if self.toast is None:self.toast=TransferCapsule(self)
        self.toast.present(record,reveal=reveal)

    def _progress(self,value):
        if self.closed or value.get('direction') not in ('send','receive'):return
        record=self.records.get(value['id'])
        if record is None and value['direction']=='receive' and value['id'] in self.offers and value.get('state') in ('connecting','transferring','reconnecting'):
            self.begin(self.offers[value['id']]);record=self.records.get(value['id'])
        if record is None or record['announced']:return
        record.update(snapshot=dict(value),received=value.get('received',record['received']),total=value.get('total',record['total']))
        if value.get('name') and not record['name']:record['name']=Path(str(value['name'])).name
        if value.get('route'):record['route']=value['route']
        previous=record['state'];state=value.get('state','');record['state']='verifying' if state=='complete' else state
        if state in ('error','cancelled','interrupted'):record['message']=str(value.get('message') or '')[:600]
        else:record['message']=''
        if record['state'] in TERMINAL and previous!=record['state']:
            record['announced']=state!='interrupted';self.current=record['id'];self._present(record,True)
        elif self.current==record['id']:self._present(record)

    def _completed(self,value):
        if self.closed or value.get('direction') not in ('send','receive'):return
        record=self.records.get(value['id'])
        if record is None and value['direction']=='receive' and value['id'] in self.offers:
            self.begin(self.offers[value['id']]);record=self.records.get(value['id'])
        if record is None or record['announced']:return
        record.update(state='complete',paths=list(value.get('paths') or record['paths']),announced=True,received=value.get('total',record['total']),message='')
        self.current=record['id'];self._present(record,True)

    def activate(self):
        record=self.records.get(self.current)
        if record is None:return
        if self.service is self.manager.wan:
            self.manager.show_wan_dialog();panel=self.manager.wan_dialog.panel
        else:
            self.manager.show_lan_dialog();panel=self.manager.lan_dialog.panel
        panel.focus_transfer(record['id'],record['snapshot'])

    def _image_ready(self,_):
        if self.toast:self.toast.update()

    def _refresh(self):
        if self.toast and self.toast.isVisible() and self.current in self.records:self._present(self.records[self.current])

    def reset(self,*_):
        self.records.clear();self.offers.clear();self.current=None;self.images.clear()
        if self.toast:self.toast.supersede()

    def close(self):
        self.closed=True;self.reset();self.images.close()
        if self.toast:self.toast.deleteLater();self.toast=None

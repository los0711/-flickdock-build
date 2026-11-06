"""Finite manual file jobs over the original Syncthing folder API."""
from pathlib import Path
import threading
import time

from manual_payload import descriptor,extract,publish
from syncthing_engine import EngineError,check_task

CONNECT_TIMEOUT_SECONDS=45
PAYLOAD_IDLE_TIMEOUT_SECONDS=120


class DirectUnavailable(ConnectionError):
    """A direct-only task can hand its application snapshot to the R2 path."""


class ReceiveJob:
    def __init__(self,engine,identifier,folder,peer,addresses,spec,*,cancel,progress,on_connected=None):
        self.engine=engine;self.identifier=check_task(identifier);self.folder=Path(folder)
        self.peer=peer;self.addresses=addresses;self.spec=descriptor(spec)
        self.cancel=cancel;self.progress=progress;self.shared=False
        self.on_connected=on_connected

    def receive(self):
        if self.cancel.is_set():raise InterruptedError('已取消传输。')
        self.engine.share(self.identifier,self.folder,self.peer,self.addresses,send=False);self.shared=True
        start=time.monotonic();last_progress=start;received=0;cursor=0;connected=False
        target=self.folder/'content.zip'
        while not self.cancel.is_set():
            # A published file is an upstream-verified rename. Its whole archive
            # SHA is checked again by extract() before any user-visible delivery.
            if target.is_file() and target.stat().st_size==self.spec['size']:
                self.progress(self.spec['size'],self.spec['size']);return target
            rows=self.engine.connections();peer=rows.get(self.peer,{})
            if not connected and peer.get('connected') is True:
                connected=True
                if self.on_connected is not None and not self.cancel.is_set():self.on_connected()
            now=time.monotonic()
            if not connected and now-start>CONNECT_TIMEOUT_SECONDS:raise DirectUnavailable('直连暂未建立。')
            if connected and now-last_progress>PAYLOAD_IDLE_TIMEOUT_SECONDS:raise DirectUnavailable('直连长时间没有收到文件数据。')
            for event in self.engine.events(cursor):
                cursor=max(cursor,event['id'])
                if event['type']=='FolderErrors' and event['data'].get('folder')==self.identifier:
                    raise EngineError('传输内核无法保存文件，请检查接收位置和磁盘空间。')
                if event['type']!='DownloadProgress':continue
                row=event['data'].get(self.identifier,{}).get('content.zip')
                if row:
                    value=min(self.spec['size'],row['bytesDone'])
                    if value>received:
                        received=value;last_progress=time.monotonic();self.progress(received,self.spec['size'])
        raise InterruptedError('已取消传输。')

    def close(self):
        if self.shared:self.engine.unshare(self.identifier);self.shared=False


def deliver(payload,stage,destination,spec,offer,cancel):
    """Publish only after archive integrity and the accepted offer both match."""
    output=Path(stage)/'verified'
    roots=extract(payload,output,spec,total=offer['total'],count=offer['count'],names=offer['names'],cancel=cancel,
                  expected_entries=offer.get('entries'),manifest_digest=offer.get('digest'))
    return publish(output,destination,roots,cancel)

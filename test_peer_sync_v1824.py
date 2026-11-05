"""Account mesh and RPC bytes use real host ICE and pinned local TLS."""
import os
import threading
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib
import json
import copy
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
from PySide6.QtCore import QObject,Signal,QTimer,QMimeData,QUrl
from PySide6.QtGui import QImage,QColor
from peer_sync import PeerSync
from peer_clipboard import ClipboardPeerBridge
from clipboard_sync import ClipboardSync
from test_clipboard_sync_v1819 import Clipboard,Events,Hub as ClipboardHub
from test_html_native_workspace import APP,flush,until


class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,group):
        super().__init__();self.signed_in=True;self.account=dict(id=group);self.device_id=str(uuid4())
        self.api=SimpleNamespace(token=uuid4().hex)


class Wan(QObject):
    peers_changed=Signal(list)
    sync_signal=Signal(dict)
    def __init__(self,hub,account,index):
        super().__init__();self.hub=hub;self.account=account;self.device_id=f'{index:064x}'
        self.base=hub.base/f'wan-{index}'
        self._connected=False;self._bound_identity=(account.device_id,account.api.token)
        self.features=['mesh-v1','routes-v1']
    def peers(self):
        return [dict(id=wan.device_id,name='QA device',online=wan._connected and wan.account.signed_in,
                     account_device_id=wan.account.device_id,features=wan.features,
                     account_device=wan.account.account['id']==self.account.account['id'])
                for wan in self.hub.wans if wan is not self]
    def _send_signal(self,value):self.hub.forward(self,value);return True


class Hub:
    def __init__(self,base):self.base=Path(base);self.wans=[];self.links={}
    def forward(self,sender,value):
        kind=value['type'];identifier=value['id']
        def emit(wan,packet):QTimer.singleShot(0,lambda:wan.sync_signal.emit(packet))
        if kind=='sync_open':
            receiver=next(wan for wan in self.wans if wan.device_id==value['peer'])
            assert receiver.account.account['id']==sender.account.account['id']
            self.links[identifier]=(sender,receiver)
            for wan,peer,initiator in ((sender,receiver,True),(receiver,sender,False)):
                emit(wan,dict(type='sync_begin',id=identifier,account=wan.account.account['id'],
                              peer=dict(id=peer.device_id),initiator=initiator,iceServers=[]))
        elif identifier in self.links:
            first,second=self.links[identifier];other=second if sender is first else first
            if kind=='sync_signal':emit(other,dict(type='sync_signal',id=identifier,payload=value['payload']))
            elif kind=='sync_lease':emit(sender,dict(type='sync_lease',id=identifier))
            elif kind=='sync_close':emit(other,dict(type='sync_closed',id=identifier))


class PeerSyncTests(unittest.TestCase):
    def test_native_error_diagnostic_retains_an_available_adaptive_connection(self):
        client=self.clients[0];state=next(iter(client._states.values()));rpc=state['rpc']
        client._handle_native(dict(state=state,event=dict(type='error',message='QA native diagnostic')))
        rows=json.loads((client.wan.base/'network-diagnostics.json').read_text(encoding='utf8'))
        self.assertEqual(rows[-1]['code'],'WEBRTC_ERROR')
        self.assertIs(state['rpc'],rpc);self.assertEqual(len(client.available()),2)

    def test_one_copy_uses_lan_p2p_and_r2_for_different_devices_without_duplicate_cloud_bodies(self):
        from peer_sync import try_nearby
        from adaptive_stream import AdaptiveStream
        cloud=ClipboardHub();syncs=[];clips=[]
        fourth=Account(self.clients[0].account.account['id']);fourth_wan=Wan(self.hub,fourth,4)
        fourth_wan.features=[];fourth_wan._connected=True;self.hub.wans.append(fourth_wan)
        lan_pair={self.clients[0].wan.device_id,self.clients[1].wan.device_id}
        def nearby(raw,role,cancel):
            first,second=self.hub.links[raw.identifier]
            return try_nearby(raw,role,cancel) if {first.device_id,second.device_id}==lan_pair else raw
        def adaptive(raw,nearby,**options):
            if nearby is not raw:options['primary']=False
            return AdaptiveStream(raw,nearby,**options)
        for mesh in self.clients:
            for peer in list(mesh._states):mesh._drop(peer,retry=False)
        flush()
        with patch('peer_sync.try_nearby',side_effect=nearby),patch('adaptive_stream.AdaptiveStream',side_effect=adaptive):
            for mesh in self.clients:mesh._reconcile()
            until(lambda:all(len(mesh.available())==2 for mesh in self.clients),seconds=20)
            self.assertEqual(self.clients[0].route_for_peer(self.clients[1].wan.device_id),'lan')
            self.assertEqual(self.clients[0].route_for_peer(self.clients[2].wan.device_id),'p2p')
            try:
                for index,account in enumerate([mesh.account for mesh in self.clients]+[fourth]):
                    token=account.api.token;api=cloud.api(account.device_id);api.token=token;account.api=api
                    events=Events();cloud.events.append(events);clip=Clipboard();clips.append(clip)
                    client=ClipboardSync(account,events,self.base/f'mixed-clipboard-{index}',clipboard=clip)
                    if index<3:client.peer_bridge=ClipboardPeerBridge(client,self.clients[index])
                    syncs.append(client)
                flush();original=self.base/'single-original-under-20MB.bin';body=os.urandom(512*1024+19);original.write_bytes(body)
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))]);clips[0].setMimeData(mime)
                until(lambda:all(clip.mimeData().hasUrls() for clip in clips),seconds=20)
                try:until(lambda:all(sum(row['success'] for row in client.view.get('deliveries',{}).values())==3 for client in syncs),seconds=10)
                except AssertionError as error:
                    raise AssertionError([(client.view.get('deliveries'),client.deliveries.cloud_ready,client.deliveries.dirty,
                                           client.deliveries.pending_route,client.deliveries.inflight) for client in syncs]) from error
                for clip in clips:self.assertEqual(Path(clip.mimeData().urls()[0].toLocalFile()).read_bytes(),body)
                for route in ('lan','p2p','cloud'):self.assertEqual(syncs[0].view['deliveries'][route],{'success':1,'total':1})
                self.assertEqual(sum(method=='GET' and path.endswith('/content') for method,path in cloud.calls),1)
                self.assertEqual(sum(method=='PUT' for method,_ in cloud.calls),1)
                self.assertEqual(original.read_bytes(),body)
            finally:
                for client in syncs:client.close()
                until(lambda:all(not client._jobs for client in syncs))

    def test_cloud_head_does_not_download_again_while_same_peer_clipboard_is_in_flight(self):
        cloud=ClipboardHub();syncs=[];clips=[];gate=threading.Event();fetches=[];feedback=[]
        for state in self.clients[0]._states.values():
            provider=state['rpc'].provider
            def delayed(topic,*args,provider=provider):
                if topic=='clipboard':gate.wait(10)
                return provider(topic,*args)
            state['rpc'].provider=delayed
        try:
            for index,mesh in enumerate(self.clients):
                account=mesh.account;api=cloud.api(account.device_id);api.token=account.api.token;account.api=api
                events=Events();cloud.events.append(events);clip=Clipboard();clips.append(clip)
                client=ClipboardSync(account,events,self.base/f'coalesced-clipboard-{index}',clipboard=clip)
                client.peer_bridge=ClipboardPeerBridge(client,mesh);syncs.append(client)
            original=self.clients[1].fetch
            def fetch(topic,record,*args,**kwargs):
                if topic=='clipboard':fetches.append(record['id'])
                return original(topic,record,*args,**kwargs)
            self.clients[1].fetch=fetch;syncs[0].copy_result.connect(lambda ok,_:feedback.append(ok))
            flush();image=QImage(240,160,QImage.Format_ARGB32);image.fill(QColor(34,67,140))
            mime=QMimeData();mime.setImageData(image);clips[0].setMimeData(mime)
            until(lambda:cloud.head is not None and cloud.head.get('ready') and bool(fetches))
            syncs[1].refresh();flush()
            self.assertEqual(len(fetches),1)
            gate.set();until(lambda:clips[1].mimeData().hasImage())
            until(lambda:syncs[0]._pending is None)
            count=len(feedback);identity=syncs[0].view['id']
            syncs[0].peer_bridge._acknowledged('clipboard',dict(id=identity,delivery_route='p2p'),self.clients[1].wan.device_id)
            self.assertEqual(syncs[0].view['route'],'p2p');self.assertTrue(syncs[0].view['backed_up'])
            self.assertEqual(len(feedback),count)
        finally:
            gate.set()
            for client in syncs:client.close()
            until(lambda:all(not client._jobs for client in syncs))

    def test_device_snapshot_uses_real_ping_round_trip_and_account_device_mapping(self):
        first,second=self.clients[:2]
        until(lambda:first.connection_snapshot().get(second.account.device_id,{}).get('latency_ms') is not None)
        info=first.connection_snapshot()[second.account.device_id]
        self.assertIn(info['route'],('lan','p2p'))
        self.assertGreaterEqual(info['latency_ms'],0);self.assertLess(info['latency_ms'],1000)

    def test_begin_before_bind_http_reply_survives_stale_presence_and_keeps_epoch_fence(self):
        first,second=self.clients[:2];peer=second.wan.device_id;identifier=uuid4().hex
        first._drop(peer,retry=False);second._drop(first.wan.device_id,retry=False);flush()
        self.hub.links[identifier]=(first.wan,second.wan)
        identity=first.wan._bound_identity;first.wan._bound_identity=None
        packet=dict(type='sync_begin',id=identifier,account=first.identity[0],peer=dict(id=peer),initiator=True,iceServers=[])
        first._signal(packet);self.assertEqual(len(first._pending_signals),1)
        second._signal(dict(packet,peer=dict(id=first.wan.device_id),initiator=False))
        original=first.wan.peers;first.wan.peers=lambda:[]
        try:
            first.wan._bound_identity=identity;first._reconcile()
            until(lambda:peer in first._states and first._states[peer]['rpc'] is not None,seconds=15)
            self.assertEqual(first._states[peer]['id'],identifier);self.assertFalse(first._pending_signals)
        finally:first.wan.peers=original

    def test_signaling_outage_preserves_authenticated_bytes_until_authorization_lease_expires(self):
        from peer_sync import AUTH_LEASE_SECONDS
        client=self.clients[0];connections={peer:state['rpc'] for peer,state in client._states.items()}
        client.wan._connected=False;client._reconcile()
        self.assertEqual(len(client.available()),2)
        self.assertEqual({peer:state['rpc'] for peer,state in client._states.items()},connections)
        body=os.urandom(128*1024+11);record=dict(id=str(uuid4()),size=len(body),sha256=hashlib.sha256(body).hexdigest())
        offers=[];self.clients[1].offered.connect(lambda *value:offers.append(value));client.publish('shelf',record,body)
        until(lambda:bool(offers));done=[];target=self.base/'signal-outage.bin'
        self.clients[1].fetch('shelf',record,target,lambda path,error:done.append((path,error)))
        until(lambda:bool(done));self.assertIsNone(done[0][1]);self.assertEqual(target.read_bytes(),body)
        for state in client._states.values():state['leased']-=AUTH_LEASE_SECONDS+1
        client._tick();self.assertFalse(client.available());self.assertFalse(client._states)

    def setUp(self):
        self.temp=TemporaryDirectory();self.base=Path(self.temp.name);self.hub=Hub(self.base);self.clients=[];group=str(uuid4())
        self.addCleanup(self._close_clients)
        for index in range(1,4):
            account=Account(group);wan=Wan(self.hub,account,index);self.hub.wans.append(wan)
            self.clients.append(PeerSync(account,wan))
        for wan in self.hub.wans:wan._connected=True;wan.peers_changed.emit(wan.peers())
        until(lambda:all(len(client.available())==2 for client in self.clients),seconds=20)
    def _close_clients(self):
        for client in self.clients:client.close()
        until(lambda:all(not client._fetches for client in self.clients))
        for client in self.clients:client.deleteLater()
        flush();self.temp.cleanup()

    @contextmanager
    def shelves(self,hold=lambda method,path:False):
        from test_shelf_sync_v180 import Server,Api,Adapter
        from shelf_sync import ShelfSync
        class SharedServer(Server):
            def dispatch(self,token,method,path,data,raw):return super().dispatch('same-account',method,path,data,raw)
        server=SharedServer();syncs=[];adapters=[];requests=[]
        try:
            for index,mesh in enumerate(self.clients):
                account=mesh.account;token=account.api.token;api=Api(server);api.token=token;original=api.request
                def request(method,path,callback,original=original,**options):
                    if hold(method,path):requests.append((original,method,path,callback,options));return uuid4().hex
                    return original(method,path,callback,**options)
                api.request=request;account.api=api;adapter=Adapter();adapters.append(adapter)
                client=ShelfSync(self.base/f'collection-{index}',account,adapter,poll_ms=600000)
                client.attach_peer(mesh);syncs.append(client);client.select_account(account.account)
            until(lambda:all(not client.running for client in syncs))
            yield server,syncs,adapters,requests
        finally:
            for client in syncs:client.close()
            until(lambda:all(not client.jobs for client in syncs))

    def test_all_online_devices_auto_connect_and_objects_arrive_without_cloud_bytes(self):
        received=[];self.clients[1].offered.connect(lambda *values:received.append(values))
        payload=os.urandom(4*1024*1024+17);source=self.base/'original.bin';source.write_bytes(payload)
        record=dict(id=str(uuid4()),size=len(payload),sha256=hashlib.sha256(payload).hexdigest())
        self.assertTrue(self.clients[0].publish('shelf',record,source))
        until(lambda:bool(received))
        result=[];target=self.base/'received.bin'
        job=self.clients[1].fetch('shelf',record,target,lambda path,error:result.append((path,error)))
        self.assertIsNotNone(job);until(lambda:bool(result),seconds=20)
        self.assertIsNone(result[0][1]);self.assertEqual(target.read_bytes(),payload)
        self.assertEqual(source.read_bytes(),payload)
        self.assertTrue(all(client.broker._process.poll() is None for client in self.clients))

    def test_hash_mismatch_never_publishes_corrupted_file_and_removes_partial(self):
        offered=[];self.clients[1].offered.connect(lambda *values:offered.append(values))
        record=dict(id=str(uuid4()),size=3,sha256=hashlib.sha256(b'ABC').hexdigest())
        self.clients[0].publish('clipboard',record,b'bad');until(lambda:bool(offered))
        result=[];target=self.base/'clipboard.bin'
        self.clients[1].fetch('clipboard',record,target,lambda path,error:result.append((path,error)))
        until(lambda:bool(result));self.assertIsNotNone(result[0][1]);self.assertFalse(target.exists())
        self.assertFalse(list(self.base.glob('*.part')))

    def test_logout_closes_all_links_and_clears_sources(self):
        client=self.clients[0];client.account.signed_in=False;client.account.account_changed.emit(None);flush()
        self.assertFalse(client.available());self.assertFalse(client._states);self.assertFalse(client._objects)
        self.assertIsNone(client.broker)

    def test_parallel_files_are_bounded_and_clipboard_keeps_a_request_slot(self):
        sender,receiver=self.clients[:2];offered=[];receiver.offered.connect(lambda *values:offered.append(values))
        records=[];bodies=[];results=[]
        for index in range(5):
            body=os.urandom(3*1024*1024+index);bodies.append(body)
            record=dict(id=str(uuid4()),size=len(body),sha256=hashlib.sha256(body).hexdigest());records.append(record)
            sender.publish('shelf',record,body)
        clip=dict(id=str(uuid4()),size=3,sha256=hashlib.sha256(b'ABC').hexdigest());sender.publish('clipboard',clip,b'ABC')
        until(lambda:len(offered)>=6)
        for index,record in enumerate(records):
            receiver.fetch('shelf',record,self.base/f'parallel-{index}',lambda path,error:results.append((path,error)))
        clip_result=[];receiver.fetch('clipboard',clip,self.base/'priority-clip',lambda path,error:clip_result.append((path,error)))
        until(lambda:len(results)==5 and bool(clip_result),seconds=20)
        self.assertTrue(all(error is None for _,error in results),results)
        self.assertIsNone(clip_result[0][1]);self.assertEqual((self.base/'priority-clip').read_bytes(),b'ABC')
        for index,body in enumerate(bodies):self.assertEqual((self.base/f'parallel-{index}').read_bytes(),body)
        self.assertFalse(receiver.available()[0]['rpc']._pending)

    def test_account_fence_cancels_inflight_bytes_and_suppresses_late_callback(self):
        sender,receiver=self.clients[:2];offered=[];receiver.offered.connect(lambda *values:offered.append(values))
        body=os.urandom(8*1024*1024);record=dict(id=str(uuid4()),size=len(body),sha256=hashlib.sha256(body).hexdigest())
        original=sender._provide
        def delayed(*args):time.sleep(.04);return original(*args)
        sender._states[receiver.wan.device_id]['rpc'].provider=delayed
        sender.publish('shelf',record,body);until(lambda:bool(offered))
        done=[];progress=[];target=self.base/'revoked.bin'
        receiver.fetch('shelf',record,target,lambda *value:done.append(value),progress=lambda *value:progress.append(value))
        until(lambda:bool(progress));receiver.account.signed_in=False;receiver.account.account_changed.emit(None)
        until(lambda:not receiver._fetches)
        self.assertFalse(done);self.assertFalse(target.exists());self.assertFalse(list(self.base.glob('*.part')))

    def test_clipboard_arrives_over_peer_before_cloud_upload_then_confirms_backup(self):
        cloud=ClipboardHub();clips=[];syncs=[];uploads=[];feedback=[]
        try:
            for index,mesh in enumerate(self.clients):
                account=mesh.account;token=account.api.token;api=cloud.api(account.device_id);api.token=token
                original=api.request
                def request(method,path,callback,original=original,**options):
                    if method=='PUT':uploads.append((original,method,path,callback,options));return object()
                    return original(method,path,callback,**options)
                api.request=request;account.api=api
                events=Events();cloud.events.append(events);clip=Clipboard();clips.append(clip)
                client=ClipboardSync(account,events,self.base/f'clipboard-{index}',clipboard=clip)
                client.peer_bridge=ClipboardPeerBridge(client,mesh);syncs.append(client)
            syncs[0].copy_result.connect(lambda ok,context:feedback.append((ok,context)))
            flush();clips[0].text('设备直连优先\n保留完整文本')
            until(lambda:all(clip.mimeData().text()=='设备直连优先\n保留完整文本' for clip in clips),seconds=10)
            self.assertIsNone(cloud.head)
            self.assertEqual(syncs[1].view['phase'],'peer');self.assertEqual(syncs[2].view['phase'],'peer')
            until(lambda:syncs[0].view['phase']=='peer')
            until(lambda:sum(row['success'] for row in syncs[0].view.get('deliveries',{}).values())==2)
            self.assertEqual(sum(row['total'] for row in syncs[0].view['deliveries'].values()),2)
            self.assertEqual(len(feedback),1);self.assertTrue(feedback[0][0]);self.assertTrue(feedback[0][1]['delivered'])
            self.assertFalse(feedback[0][1]['backed_up']);self.assertIn(feedback[0][1]['route'],('lan','p2p','relay'))
            self.assertIn(syncs[1].view['route'],('lan','p2p','relay'))
            until(lambda:bool(uploads))
            self.assertEqual(len(uploads),1)
            for original,method,path,callback,options in uploads:original(method,path,callback,**options)
            until(lambda:all(client.view['phase']=='synced' for client in syncs),seconds=10)
            self.assertTrue(syncs[1].view['backed_up']);self.assertIn(syncs[1].view['route'],('lan','p2p','relay'))
            self.assertEqual(sum(method=='PUT' for method,_ in cloud.calls),1)
            until(lambda:all(sum(row['success'] for row in client.view.get('deliveries',{}).values())==2 for client in syncs))
            self.assertEqual(sum(method=='GET' and path.endswith('/content') for method,path in cloud.calls),0)
            self.assertEqual(len(feedback),1)
        finally:
            for client in syncs:client.close()
            until(lambda:all(not client._jobs for client in syncs))

    def test_shelf_file_arrives_before_cloud_parts_and_commits_without_duplicate(self):
        from test_shelf_sync_v180 import Server,Api,Adapter
        from shelf_sync import ShelfSync
        class SharedServer(Server):
            def dispatch(self,token,method,path,data,raw):return super().dispatch('same-account',method,path,data,raw)
        server=SharedServer();syncs=[];adapters=[];uploads=[];released=[False]
        try:
            for index,mesh in enumerate(self.clients):
                account=mesh.account;token=account.api.token;api=Api(server);api.token=token;original=api.request
                def request(method,path,callback,original=original,**options):
                    if method=='PUT' and not released[0]:uploads.append((original,method,path,callback,options));return uuid4().hex
                    return original(method,path,callback,**options)
                api.request=request;account.api=api;adapter=Adapter();adapters.append(adapter)
                client=ShelfSync(self.base/f'shelf-{index}',account,adapter,poll_ms=600000)
                client.attach_peer(mesh);syncs.append(client);client.select_account(account.account)
            until(lambda:all(not client.running for client in syncs))
            payload=os.urandom(129*1024+17);original=self.base/'original-shelf.bin';original.write_bytes(payload)
            adapters[0].add(str(original));syncs[0].local_changed();syncs[0].tick()
            until(lambda:all(len(adapter.view['permanent'])==1 for adapter in adapters),seconds=15)
            self.assertFalse(server.account('same-account')['items']);self.assertTrue(uploads)
            for client,adapter in zip(syncs[1:],adapters[1:]):
                ref=adapter.view['permanent'][0];self.assertEqual(Path(ref).read_bytes(),payload)
                self.assertEqual(client.sync_status_for_ref(ref),'peer')
            released[0]=True
            for request,method,path,callback,options in uploads:request(method,path,callback,**options)
            until(lambda:bool(server.account('same-account')['items']) and not syncs[0].running,seconds=15)
            until(lambda:all(not next(iter(client.state['entries'].values())).get('peer_provisional') for client in syncs[1:]))
            self.assertTrue(all(len(adapter.view['permanent'])==1 for adapter in adapters))
            self.assertEqual(original.read_bytes(),payload)
        finally:
            for client in syncs:client.close()

    def test_shelf_text_folder_and_deletion_arrive_over_peers(self):
        blocked={'parts':True,'delete':False}
        with self.shelves(lambda method,path:method=='PUT' and blocked['parts'] or
                          method=='DELETE' and path.startswith('/v1/items/') and blocked['delete']) as (server,syncs,adapters,held):
            folder=self.base/'资料';folder.mkdir();(folder/'笔记.txt').write_text('目录原始内容',encoding='utf8')
            adapters[0].add('flickdock:text:qa','完整的直连文字\n第二行')
            adapters[0].view['metadata']['flickdock:text:qa']={'title':'完整的直连文字'}
            syncs[0].local_changed();syncs[0].tick()
            until(lambda:all(len(adapter.view['permanent'])==1 for adapter in adapters),seconds=15)
            self.assertFalse(server.account('same-account')['items'])
            blocked['parts']=False
            for request,method,path,callback,options in held[:]:request(method,path,callback,**options)
            held.clear()
            until(lambda:not syncs[0].running and len(server.account('same-account')['items'])==1)
            blocked['parts']=True;adapters[0].add(str(folder));syncs[0].local_changed();syncs[0].tick()
            until(lambda:all(len(adapter.view['permanent'])==2 for adapter in adapters),seconds=15)
            self.assertEqual(len(server.account('same-account')['items']),1)
            for adapter in adapters[1:]:
                self.assertIn('完整的直连文字\n第二行',adapter.view['texts'].values())
                received=next(Path(ref) for ref in adapter.view['permanent'] if not ref.startswith('flickdock:text:'))
                self.assertEqual((received/'笔记.txt').read_text(encoding='utf8'),'目录原始内容')
            blocked['parts']=False
            for request,method,path,callback,options in held[:]:request(method,path,callback,**options)
            held.clear()
            until(lambda:len(server.account('same-account')['items'])==2 and not syncs[0].running,seconds=15)
            until(lambda:all(not entry.get('peer_provisional') for client in syncs for entry in client.state['entries'].values()))
            self.assertTrue(list((syncs[0].root/syncs[0].account_id/'peer-cache').glob('*.blob')))
            blocked['delete']=True;adapters[0].view={'permanent':[],'texts':{},'metadata':{}}
            syncs[0].local_changed();syncs[0].tick()
            until(lambda:all(not adapter.view['permanent'] for adapter in adapters))
            self.assertTrue(all(not item['record']['deleted'] for item in server.account('same-account')['items'].values()))
            blocked['delete']=False
            for request,method,path,callback,options in held[:]:request(method,path,callback,**options)
            until(lambda:all(item['record']['deleted'] for item in server.account('same-account')['items'].values()))
            self.assertTrue((folder/'笔记.txt').exists())

    def test_uncommitted_removal_does_not_leave_or_resurrect_peer_copy(self):
        with self.shelves(lambda method,path:method=='PUT') as (server,syncs,adapters,held):
            source=self.base/'remove-before-cloud.bin';source.write_bytes(b'original data')
            adapters[0].add(str(source));syncs[0].local_changed();syncs[0].tick()
            until(lambda:all(adapter.view['permanent'] for adapter in adapters))
            old=copy.deepcopy(next(iter(self.clients[1]._offers.values()))[1])
            adapters[0].view['permanent']=[];syncs[0].local_changed();syncs[0].tick()
            until(lambda:all(not adapter.view['permanent'] for adapter in adapters))
            self.clients[1]._queue_offer(self.clients[2].wan.device_id,'shelf',old);flush()
            self.assertFalse(adapters[2].view['permanent']);self.assertEqual(source.read_bytes(),b'original data')

    def test_authoritative_conflict_preserves_both_actual_bodies(self):
        with self.shelves(lambda method,path:method=='PUT') as (server,syncs,adapters,held):
            source=self.base/'conflict.txt';source.write_bytes(b'peer version')
            adapters[0].add(str(source));syncs[0].local_changed();syncs[0].tick()
            until(lambda:all(adapter.view['permanent'] for adapter in adapters))
            client=syncs[1];entry=next(iter(client.state['entries'].values()));old_ref=entry['ref']
            payload=b'cloud winner';record=dict(id=entry['id'],version=entry['version'],kind='file',name='conflict.txt',
                size=len(payload),sha256=hashlib.sha256(payload).hexdigest(),mime_type='text/plain',deleted=False)
            server.account('same-account')['items'][record['id']]=dict(record=record,data=payload)
            done=[];client._receive(record,lambda:done.append(True))
            until(lambda:bool(done));self.assertEqual(len(adapters[1].view['permanent']),2)
            self.assertIn(old_ref,adapters[1].view['permanent'])
            self.assertEqual({Path(ref).read_bytes() for ref in adapters[1].view['permanent']},{b'peer version',payload})

    def test_clipboard_image_and_original_file_use_peer_bytes_with_cloud_held(self):
        cloud=ClipboardHub();clips=[];syncs=[];held=[]
        try:
            for index,mesh in enumerate(self.clients):
                account=mesh.account;api=cloud.api(account.device_id);api.token=account.api.token;original=api.request
                def request(method,path,callback,original=original,**options):
                    if method=='PUT':held.append((method,path));return object()
                    return original(method,path,callback,**options)
                api.request=request;account.api=api;events=Events();cloud.events.append(events);clip=Clipboard();clips.append(clip)
                client=ClipboardSync(account,events,self.base/f'binary-clipboard-{index}',clipboard=clip)
                client.peer_bridge=ClipboardPeerBridge(client,mesh);syncs.append(client)
            flush();image=QImage(120,80,QImage.Format_ARGB32);image.fill(QColor(20,45,160,210))
            mime=QMimeData();mime.setImageData(image);clips[0].setMimeData(mime)
            until(lambda:all(clip.mimeData().hasImage() for clip in clips))
            for clip in clips:self.assertEqual(clip.mimeData().imageData().convertToFormat(QImage.Format_RGBA8888),image.convertToFormat(QImage.Format_RGBA8888))
            path=self.base/'原始文件.bin';payload=os.urandom(133*1024);path.write_bytes(payload)
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);clips[0].setMimeData(mime)
            until(lambda:all(clip.mimeData().hasUrls() for clip in clips))
            for clip in clips:self.assertEqual(Path(clip.mimeData().urls()[0].toLocalFile()).read_bytes(),payload)
            self.assertIsNone(cloud.head);self.assertEqual(path.read_bytes(),payload)
        finally:
            for client in syncs:client.close()
            until(lambda:all(not client._jobs for client in syncs))


if __name__=='__main__':unittest.main(verbosity=2)
